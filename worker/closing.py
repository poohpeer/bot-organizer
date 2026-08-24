import bot.session as session

_CLOSING_QUESTION_TEXT = "Ну как всё прошло? Я вам ещё нужен или можно закрывать?"
_AUTO_CLOSE_NOTICE = "Не дождался ответа — закрываю сессию сам."


async def fire_closing_questions(pool, telegram_bot) -> list[int]:
    # Claimed (and marked) atomically before sending — see the interface
    # note above. A send that fails costs one skipped question rather than
    # a duplicated one.
    claimed = await session.claim_sessions_for_closing_question(pool)
    asked = []
    for row in claimed:
        await telegram_bot.send_message(chat_id=row["chat_id"], text=_CLOSING_QUESTION_TEXT)
        asked.append(row["id"])
    return asked


async def fire_auto_closes(pool, telegram_bot) -> list[int]:
    claimed = await session.claim_sessions_for_auto_close(pool)
    closed = []
    for row in claimed:
        await telegram_bot.send_message(chat_id=row["chat_id"], text=_AUTO_CLOSE_NOTICE)
        closed.append(row["id"])
    return closed
