import logging

import bot.session as session

log = logging.getLogger(__name__)

_CLOSING_QUESTION_TEXT = "Ну как всё прошло? Я вам ещё нужен или можно закрывать?"
_AUTO_CLOSE_NOTICE = "Не дождался ответа — закрываю сессию сам."


async def fire_closing_questions(pool, telegram_bot) -> list[int]:
    """Ask every session that is due whether it can be closed.

    Claimed atomically before sending so two pollers can't both ask, but a
    failed send is put back: a session left marked as "asked" when nothing was
    sent would auto-close two days later without the group ever having been
    asked. Each chat is isolated so one unreachable group doesn't strand the
    rest of the batch.
    """
    asked = []
    for row in await session.claim_sessions_for_closing_question(pool):
        try:
            await telegram_bot.send_message(chat_id=row["chat_id"], text=_CLOSING_QUESTION_TEXT)
        except Exception:
            log.warning("Could not ask the closing question in chat %s", row["chat_id"], exc_info=True)
            await session.release_closing_question_claim(
                pool, row["id"], row["prev_asked_at"], row["prev_retries"]
            )
            continue
        asked.append(row["id"])
    return asked


async def fire_auto_closes(pool, telegram_bot) -> list[int]:
    """Close sessions whose closing question went unanswered twice.

    A failed send is *not* rolled back here: the close itself is correct and
    already committed, only the courtesy notice was lost. Reopening the session
    would put it straight back into the auto-close queue and re-close it on the
    next poll.
    """
    closed = []
    for row in await session.claim_sessions_for_auto_close(pool):
        closed.append(row["id"])
        try:
            await telegram_bot.send_message(chat_id=row["chat_id"], text=_AUTO_CLOSE_NOTICE)
        except Exception:
            log.warning("Closed session %s but could not post the notice", row["id"], exc_info=True)
    return closed
