"""Telegram application wiring: the long-polling process entrypoint that
turns every incoming update into a call into `bot.router`.
"""

import logging
import os

from telegram import (
    BotCommand, BotCommandScopeAllChatAdministrators, BotCommandScopeDefault,
    Update,
)
from telegram.constants import ChatMemberStatus
from telegram.ext import (
    Application, CallbackQueryHandler, ChatMemberHandler, CommandHandler,
    ContextTypes, MessageHandler, filters,
)

import bot.admin as admin
import bot.dedup as dedup
import bot.mcp_server as mcp_server
import bot.router as router
import bot.session as session
import bot.status_command as status_command
import bot.tools.core as core_tools
from bot.tools.schema import ALL_TOOLS
import db.pool as db_pool_module
from bot.logging_setup import configure_logging, truncate

configure_logging()
log = logging.getLogger(__name__)

# Mirrors addressing.bot_was_added's _PRESENT set, inverted: any status that
# means the bot is no longer in the chat at all. RESTRICTED is deliberately
# excluded, same as there — a restriction is not a removal.
_ABSENT = frozenset({ChatMemberStatus.LEFT, ChatMemberStatus.BANNED})


def _bot_was_removed(chat_member_updated, bot_id: int) -> bool:
    new = chat_member_updated.new_chat_member
    if new.user.id != bot_id:
        return False
    old = chat_member_updated.old_chat_member
    return new.status in _ABSENT and old.status not in _ABSENT


_NEW_MEMBER_TEXT = "У нас новый участник — {name}. Я добавил его в список, жду подтверждения."


def _display_name_of_new_member(user) -> str:
    return user.full_name or (f"@{user.username}" if user.username else str(user.id))


async def handle_new_members(pool, telegram_bot, message) -> None:
    """Record each newly joined human as a participant and say so.

    `new_chat_members` plus someone speaking are the *only* roster growth the
    Bot API allows at all (see EPIC.md's "What the Telegram Bot API cannot
    do" — there is still no way to list who is already in the chat). A
    dormant chat is not being tracked (R5): a join there is recorded nowhere
    and announced to nobody, so the active-session check happens before a
    single participant row is touched. Bots joining are ignored for the same
    reason route_update already drops messages *sent* by a bot — a bot in the
    roster is never a person to nudge for confirmation.
    """
    active = await session.get_active_session(pool, message.chat.id)
    if active is None:
        return
    for member in message.new_chat_members:
        if member.is_bot:
            continue
        display_name = _display_name_of_new_member(member)
        existing = await core_tools.get_participant_status(pool, active["id"], user_id=member.id)
        if existing is not None:
            # A rejoin — someone who left and came back, or Telegram simply
            # redelivering new_chat_members. set_participant with a fixed
            # "unknown" status would silently overwrite a real "confirmed" or
            # "declined" answer and then announce "жду подтверждения" about
            # someone who already answered, which is both false and destroys
            # the confirmation that had already been recorded.
            continue
        await core_tools.set_participant(
            pool, active["id"], display_name, "unknown", user_id=member.id
        )
        await telegram_bot.send_message(
            chat_id=message.chat.id, text=_NEW_MEMBER_TEXT.format(name=display_name)
        )


async def route_update(pool, telegram_bot, message, bot_id, bot_username, *, update_id) -> None:
    # One line per incoming message, before anything can drop it. When someone
    # reports "the bot didn't answer", this is where you look first: either the
    # message never arrived, or one of the returns below says exactly why
    # nothing happened.
    log.debug(
        "update %s from chat %s (%s) user=%s: %s",
        update_id, message.chat.id, message.chat.type,
        message.from_user.id if message.from_user else None,
        truncate(message.text or message.caption or "<no text>"),
    )
    if await dedup.is_duplicate(pool, update_id):
        log.debug("update %s: dropped, already seen", update_id)
        return
    if message.from_user is not None and message.from_user.is_bot:
        # Two bots addressing each other would otherwise loop forever.
        log.debug("update %s: dropped, sender is a bot", update_id)
        return

    if message.new_chat_members:
        # A join is a service message, not ordinary chat — it must never
        # reach handle_dormant_message/handle_active_message, which would
        # otherwise spend a model call trying to extract meaning from it.
        log.debug("update %s: %d new member(s) in chat %s",
                   update_id, len(message.new_chat_members), message.chat.id)
        await handle_new_members(pool, telegram_bot, message)
        return

    # A private chat carries no session of its own; it is where participants
    # answer the nudge R2 sends them. Only if this isn't such an answer does it
    # fall through to ordinary handling.
    if message.chat.type == "private":
        if await router.handle_private_message(pool, telegram_bot, message):
            log.debug("update %s: handled as a participant's DM reply", update_id)
            return

    active = await session.get_active_session(pool, message.chat.id)
    if active is None:
        log.debug("update %s: chat is dormant", update_id)
        await router.handle_dormant_message(pool, telegram_bot, message, bot_id, bot_username)
    else:
        log.debug("update %s: active session %s", update_id, active["id"])
        # A location carries no text, so the ordinary path would classify it
        # as an empty message and forget the pin.
        if await router.handle_shared_location(pool, active, message, bot_id, bot_username):
            log.debug("update %s: handled as a shared location", update_id)
            return
        # A navigator link is taken as given — no lookup, no question. See
        # router.handle_shared_map_link.
        if await router.handle_shared_map_link(pool, telegram_bot, active, message):
            log.debug("update %s: handled as a shared map link", update_id)
            return
        await router.handle_active_message(pool, telegram_bot, active, message, bot_id, bot_username)


async def route_membership(pool, telegram_bot, chat_member_updated, bot_id, bot_username, *, update_id) -> None:
    # The story's produced signature for this function omits update_id, but
    # R10 requires dedup "before anything with side effects... membership
    # updates included" — unreachable without it. Adding the parameter here,
    # matching route_update's pattern, so the guarantee is actually testable.
    log.info("membership update %s in chat %s: %s -> %s",
             update_id, chat_member_updated.chat.id,
             chat_member_updated.old_chat_member.status,
             chat_member_updated.new_chat_member.status)
    if await dedup.is_duplicate(pool, update_id):
        log.debug("membership update %s: dropped, already seen", update_id)
        return

    await router.handle_bot_added(pool, telegram_bot, chat_member_updated, bot_id, bot_username)

    if _bot_was_removed(chat_member_updated, bot_id):
        # Without this the worker keeps trying to ask its closing question in
        # a chat it can no longer reach, forever.
        active = await session.get_active_session(pool, chat_member_updated.chat.id)
        if active is not None:
            log.info("Bot removed from chat %s, closing session %s",
                     chat_member_updated.chat.id, active["id"])
            await session.close_session(pool, active["id"], reason="explicit_stop")


# What the slash menu offers, in the order it shows them. Descriptions are
# what a person sees while typing, so they say what they get rather than
# naming the machinery.
#
# /admin is offered to administrators only. That is the menu, not the gate:
# admin.is_chat_admin still asks Telegram on the command and again on every
# button press, because a command absent from the menu can still be typed.
# Hiding it stops it being suggested to ten people who cannot use it.
PUBLIC_COMMANDS = [
    BotCommand("start", "Что я умею"),
    BotCommand("status", "Что известно о встрече"),
    BotCommand("list", "Список покупок"),
    BotCommand("reminders", "Что запланировано"),
]
ADMIN_COMMANDS = PUBLIC_COMMANDS + [
    BotCommand("admin", "Настройки бота"),
]

# Scope -> what that scope sees. Telegram falls back from the narrowest
# matching scope outwards, so administrators get ADMIN_COMMANDS and everyone
# else falls through to the default.
COMMAND_SCOPES = (
    (BotCommandScopeDefault(), PUBLIC_COMMANDS),
    (BotCommandScopeAllChatAdministrators(), ADMIN_COMMANDS),
)


async def on_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    bot_data = context.bot_data
    await route_update(
        bot_data["pool"], context.bot, update.effective_message,
        bot_data["bot_id"], bot_data["bot_username"], update_id=update.update_id,
    )


async def on_my_chat_member(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    bot_data = context.bot_data
    await route_membership(
        bot_data["pool"], context.bot, update.my_chat_member,
        bot_data["bot_id"], bot_data["bot_username"], update_id=update.update_id,
    )


# What /start answers. Telegram sends it automatically the first time anyone
# opens a private chat with a bot, so this is the first thing many people
# ever read from it — it says what the bot is for and how to set it going,
# and nothing about how it works inside.
#
# The same text in a group and in private: the bot is started the same way in
# both, and a person who read one and then tried the other would be told two
# different things about one bot.
START_TEXT = (
    "Я помогаю группе собраться: помню место и дату, веду список покупок, "
    "отмечаю кто идёт и напоминаю о чём просили.\n\n"
    "Чтобы начать — упомяните меня и скажите, что организуем: "
    "«@{username} едем на шашлыки в субботу».\n\n"
    "Дальше можно просто писать в чат, я читаю и запоминаю. "
    "Обращайтесь ко мне, когда нужен ответ.\n\n"
    "/status — что известно о встрече\n"
    "/list — список покупок\n"
    "/reminders — что запланировано"
)


async def on_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if update.message is None:
        return
    await context.bot.send_message(
        chat_id=update.message.chat.id,
        text=START_TEXT.format(username=context.bot_data["bot_username"]),
    )


async def on_status(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if update.message is None:
        return
    await status_command.handle_command(
        context.bot_data["pool"], context.bot, update.message, "status"
    )


async def on_list(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if update.message is None:
        return
    await status_command.handle_command(
        context.bot_data["pool"], context.bot, update.message, "list"
    )


async def on_reminders(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if update.message is None:
        return
    await status_command.handle_command(
        context.bot_data["pool"], context.bot, update.message, "reminders"
    )


async def on_pick_chat(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if update.callback_query is None:
        return
    await status_command.handle_pick(
        context.bot_data["pool"], context.bot, update.callback_query
    )


async def on_admin(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if update.message is None:
        return
    await admin.handle_command(context.bot_data["pool"], context.bot, update.message)


async def on_admin_button(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if update.callback_query is None:
        return
    await admin.handle_callback(
        context.bot_data["pool"], context.bot, update.callback_query
    )


async def post_init(app: Application) -> None:
    pool = await db_pool_module.create_pool(os.environ["DATABASE_URL"])
    await db_pool_module.init_db(pool)
    # Resolved fresh at startup, never a constant/env var: a stale username
    # after a rename would make every @mention stop matching and the bot
    # would go permanently silent with nothing in the logs to explain why.
    me = await app.bot.get_me()
    app.bot_data["pool"] = pool
    app.bot_data["bot_id"] = me.id
    app.bot_data["bot_username"] = me.username
    log.info("Resolved bot identity: id=%s username=%s", me.id, me.username)

    # Registered from code, not from BotFather. Telegram keeps whatever was
    # set last, forever and invisibly: the only command it was offering was
    # /ask_everyone, typed into BotFather at some point and backed by nothing
    # in this repository — so the slash menu advertised a command that did
    # nothing and hid three that work. set_my_commands replaces the whole
    # list, which is what makes this the single source.
    try:
        for scope, commands in COMMAND_SCOPES:
            await app.bot.set_my_commands(commands, scope=scope)
        log.info("Registered the command list for %d scopes", len(COMMAND_SCOPES))
    except Exception:
        # A failure here costs autocomplete, not the bot. Starting anyway
        # beats refusing to run because a cosmetic call was rate limited.
        log.warning("Could not register the command list", exc_info=True)

    # Started here rather than as its own process: the endpoint dispatches
    # into the same registry and the same pool this one already holds, and a
    # second process would need its own copy of both. Kept on the runner so
    # post_shutdown can close it rather than leaving a listening socket
    # behind on a restart.
    app.bot_data["mcp_grants"] = mcp_server.GrantStore()
    # The router issues grants; it learns where to get them from here rather
    # than importing this module, which would be a cycle.
    router.set_grant_store(app.bot_data["mcp_grants"])
    app.bot_data["mcp_runner"] = await mcp_server.serve(
        lambda session_id, current_user: router._build_registry(
            pool, app.bot, session_id=session_id, current_user=current_user,
        ),
        ALL_TOOLS.function_declarations,
        app.bot_data["mcp_grants"],
    )


async def post_shutdown(app: Application) -> None:
    runner = app.bot_data.get("mcp_runner")
    if runner is not None:
        await runner.cleanup()


def main() -> None:
    app = (
        Application.builder()
        .token(os.environ["BOT_ORGANIZER_BOT_TOKEN"])
        .concurrent_updates(True)
        .post_init(post_init)
        .post_shutdown(post_shutdown)
        .build()
    )
    app.add_handler(CommandHandler("start", on_start))
    app.add_handler(CommandHandler("status", on_status))
    app.add_handler(CommandHandler("list", on_list))
    app.add_handler(CommandHandler("reminders", on_reminders))
    app.add_handler(CommandHandler("admin", on_admin))
    app.add_handler(CallbackQueryHandler(on_admin_button, pattern=r"^adm:"))
    app.add_handler(CallbackQueryHandler(on_pick_chat, pattern=r"^dm:"))
    app.add_handler(MessageHandler(filters.ALL & ~filters.COMMAND, on_message))
    app.add_handler(ChatMemberHandler(on_my_chat_member, ChatMemberHandler.MY_CHAT_MEMBER))
    log.info("Starting bot (long polling)")
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
