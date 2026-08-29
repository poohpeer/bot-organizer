"""/status and /list — the organizing state on demand, read-only.

The same fixed blocks bot.tools.composed.event_status and
bot.tools.core.list_show build for the model, delivered without a model call
at all. Asking "как дела с организацией" spends a turn of the primary chain
and depends on the model choosing to relay the report verbatim
(bot.turn_outcome enforces that, which is itself evidence it does not always
happen). A command has neither cost nor that failure mode.

No admin check, unlike /admin: these only read, and the report is about the
group's own event — every member can already see all of it in the chat.

In a private chat there is no session to read, so bot.dm decides which event
the question is about and whether this person may see it.
"""

import logging

import bot.dm as dm
import bot.session as session
import bot.telegram_text as telegram_text
import bot.tools.composed as composed_tools
import bot.tools.core as core_tools

log = logging.getLogger(__name__)

NO_SESSION = (
    "Я пока ничего не отслеживаю в этом чате. Упомяните меня и скажите, "
    "что организуем."
)


async def _status_text(pool, session_id: int) -> str | None:
    result = await composed_tools.event_status(pool, session_id)
    # unknown_session is unreachable here — the session was just read — but
    # answering with a KeyError traceback if that ever changes is worse than
    # one branch.
    report = result.get("report")
    if not report:
        log.warning("event_status gave no report for session %s", session_id)
    return report


async def _list_text(pool, session_id: int) -> str | None:
    return (await core_tools.list_show(pool, session_id)).get("rendered")


# The two things a person can ask for, by the verb that rides in the
# keyboard's callback data. Adding a third means adding it here and nowhere
# else.
ANSWERS = {"status": _status_text, "list": _list_text}


async def _answer(pool, telegram_bot, chat_id: int, session_id: int, verb: str) -> None:
    text = await ANSWERS[verb](pool, session_id)
    # send_text, not send_message: the report may carry a map link, and this
    # is one of the two places a report can leave the process.
    await telegram_text.send_text(telegram_bot, chat_id, text or NO_SESSION)


async def handle_command(pool, telegram_bot, message, verb: str = "status") -> None:
    chat_id = message.chat.id
    if getattr(message.chat, "type", None) == "private":
        await _handle_private(pool, telegram_bot, message, verb)
        return

    active = await session.get_active_session(pool, chat_id)
    if active is None:
        await telegram_bot.send_message(chat_id=chat_id, text=NO_SESSION)
        return
    await _answer(pool, telegram_bot, chat_id, active["id"], verb)


async def _handle_private(pool, telegram_bot, message, verb: str) -> None:
    """Asked privately, so nobody else is disturbed — which is the point.

    "You have none" and "which of these" are different answers, so resolve()
    reporting None is not enough on its own.
    """
    user_id = message.from_user.id if message.from_user else None
    chat_id = message.chat.id
    if user_id is None:
        await telegram_bot.send_message(chat_id=chat_id, text=dm.NOTHING_TRACKED)
        return

    rows = await dm.sessions_for(pool, telegram_bot, user_id)
    if not rows:
        await telegram_bot.send_message(chat_id=chat_id, text=dm.NOTHING_TRACKED)
        return
    if len(rows) > 1:
        await telegram_bot.send_message(
            chat_id=chat_id, text=dm.PICK_A_CHAT,
            reply_markup=dm.pick_keyboard(rows, verb),
        )
        return
    await _answer(pool, telegram_bot, chat_id, rows[0]["id"], verb)


async def handle_pick(pool, telegram_bot, query) -> None:
    """A button press on the "which event?" keyboard.

    The session id in the callback data is not a permission. The keyboard
    stays live in the private chat and membership can end after it was drawn,
    so it is checked again here.
    """
    data = query.data or ""
    user_id = query.from_user.id if query.from_user else None
    chat_id = query.message.chat.id if query.message else None
    if chat_id is None or user_id is None or not data.startswith(dm.PICK):
        await query.answer()
        return

    verb, _, raw_id = data[len(dm.PICK):].partition(":")
    if verb not in ANSWERS or not raw_id.isdigit():
        await query.answer()
        return

    row = await dm.session_if_allowed(pool, telegram_bot, user_id, int(raw_id))
    if row is None:
        await query.answer(dm.NOTHING_TRACKED, show_alert=True)
        return

    await query.answer()
    await _answer(pool, telegram_bot, chat_id, row["id"], verb)
