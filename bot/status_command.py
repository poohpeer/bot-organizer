"""/status — the organizing report, on demand.

The same fixed block bot.tools.composed.event_status builds for the model,
delivered without a model call at all. Asking "как дела с организацией" spends
a turn of the primary chain and depends on the model choosing to relay the
report verbatim (bot.turn_outcome enforces that, which is itself evidence it
does not always happen). A command has neither cost nor that failure mode.

No admin check, unlike /admin: this only reads, and the report is about the
group's own event — every member can already see all of it in the chat.
"""

import logging

import bot.session as session
import bot.tools.composed as composed_tools

log = logging.getLogger(__name__)

NO_SESSION = (
    "Я пока ничего не отслеживаю в этом чате. Упомяните меня и скажите, "
    "что организуем."
)


async def handle_command(pool, telegram_bot, message) -> None:
    chat_id = message.chat.id
    active = await session.get_active_session(pool, chat_id)
    if active is None:
        await telegram_bot.send_message(chat_id=chat_id, text=NO_SESSION)
        return

    result = await composed_tools.event_status(pool, active["id"])
    # unknown_session is unreachable here — the session was just read — but
    # answering with a KeyError traceback if that ever changes is worse than
    # one branch.
    report = result.get("report")
    if not report:
        log.warning("event_status gave no report for session %s", active["id"])
        await telegram_bot.send_message(chat_id=chat_id, text=NO_SESSION)
        return

    await telegram_bot.send_message(chat_id=chat_id, text=report)
