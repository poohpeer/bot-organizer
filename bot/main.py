"""Telegram application wiring: the long-polling process entrypoint that
turns every incoming update into a call into `bot.router`.
"""

import logging
import os

from telegram import Update
from telegram.constants import ChatMemberStatus
from telegram.ext import Application, ChatMemberHandler, ContextTypes, MessageHandler, filters

import bot.dedup as dedup
import bot.router as router
import bot.session as session
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


def main() -> None:
    app = (
        Application.builder()
        .token(os.environ["BOT_TOKEN"])
        .concurrent_updates(True)
        .post_init(post_init)
        .build()
    )
    app.add_handler(MessageHandler(filters.ALL & ~filters.COMMAND, on_message))
    app.add_handler(ChatMemberHandler(on_my_chat_member, ChatMemberHandler.MY_CHAT_MEMBER))
    log.info("Starting bot (long polling)")
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
