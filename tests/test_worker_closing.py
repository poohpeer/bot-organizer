from unittest.mock import AsyncMock

import bot.session as session
import worker.closing as worker_closing


async def _overdue_session(db_pool, chat_id=1):
    await db_pool.execute("INSERT INTO chats (chat_id, title) VALUES ($1, 'Chat') ON CONFLICT DO NOTHING", chat_id)
    row = await session.start_session(db_pool, chat_id=chat_id, activity_type="picnic")
    await db_pool.execute("UPDATE sessions SET event_date = current_date - 1 WHERE id = $1", row["id"])
    return row["id"]


async def test_fire_closing_questions_asks_and_marks(db_pool):
    session_id = await _overdue_session(db_pool)
    telegram_bot = AsyncMock()

    asked = await worker_closing.fire_closing_questions(db_pool, telegram_bot)

    assert asked == [session_id]
    telegram_bot.send_message.assert_awaited_once()
    assert telegram_bot.send_message.await_args.kwargs["chat_id"] == 1
    row = await db_pool.fetchrow("SELECT closing_question_asked_at FROM sessions WHERE id = $1", session_id)
    assert row["closing_question_asked_at"] is not None


async def test_fire_closing_questions_skips_sessions_not_due(db_pool):
    await db_pool.execute("INSERT INTO chats (chat_id, title) VALUES (1, 'Chat')")
    await session.start_session(db_pool, chat_id=1, activity_type="picnic")
    telegram_bot = AsyncMock()

    asked = await worker_closing.fire_closing_questions(db_pool, telegram_bot)

    assert asked == []
    telegram_bot.send_message.assert_not_awaited()


async def test_fire_auto_closes_closes_and_notifies(db_pool):
    session_id = await _overdue_session(db_pool)
    await db_pool.execute(
        """
        UPDATE sessions SET closing_question_asked_at = now() - interval '3 days',
                             closing_question_retries = 1
        WHERE id = $1
        """,
        session_id,
    )
    telegram_bot = AsyncMock()

    closed = await worker_closing.fire_auto_closes(db_pool, telegram_bot)

    assert closed == [session_id]
    telegram_bot.send_message.assert_awaited_once()
    row = await db_pool.fetchrow("SELECT status, closed_reason FROM sessions WHERE id = $1", session_id)
    assert row["status"] == "closed"
    assert row["closed_reason"] == "auto_close_silence"


async def test_fire_auto_closes_skips_sessions_not_due(db_pool):
    session_id = await _overdue_session(db_pool)
    telegram_bot = AsyncMock()

    closed = await worker_closing.fire_auto_closes(db_pool, telegram_bot)

    assert closed == []
    telegram_bot.send_message.assert_not_awaited()
