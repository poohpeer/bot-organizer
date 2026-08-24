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


async def test_one_unreachable_chat_does_not_strand_the_others(db_pool):
    """The loop aborted on the first failure, so later chats never even got an
    attempt — yet the claim had already marked every one of them as asked."""
    ids = []
    for chat_id in (1, 2, 3):
        await db_pool.execute("INSERT INTO chats (chat_id, title) VALUES ($1, 'Chat')", chat_id)
        ids.append(await db_pool.fetchval(
            """
            INSERT INTO sessions (chat_id, activity_type, event_date)
            VALUES ($1, 'picnic', current_date - 2) RETURNING id
            """,
            chat_id,
        ))
    telegram_bot = AsyncMock()

    def refuse_the_first(chat_id, text):
        if chat_id == 1:
            raise RuntimeError("Forbidden: bot was kicked from the group")

    telegram_bot.send_message.side_effect = lambda **kw: refuse_the_first(kw["chat_id"], kw["text"])

    asked = await worker_closing.fire_closing_questions(db_pool, telegram_bot)

    assert asked == ids[1:]
    assert telegram_bot.send_message.await_count == 3


async def test_a_question_that_could_not_be_sent_is_not_recorded_as_asked(db_pool):
    """R4: a session marked as asked but never actually asked auto-closes two
    days later without the group having been consulted at all."""
    await db_pool.execute("INSERT INTO chats (chat_id, title) VALUES (1, 'Chat')")
    session_id = await db_pool.fetchval(
        """
        INSERT INTO sessions (chat_id, activity_type, event_date)
        VALUES (1, 'picnic', current_date - 2) RETURNING id
        """
    )
    telegram_bot = AsyncMock()
    telegram_bot.send_message.side_effect = RuntimeError("Forbidden")

    assert await worker_closing.fire_closing_questions(db_pool, telegram_bot) == []

    row = await db_pool.fetchrow(
        "SELECT closing_question_asked_at, closing_question_retries FROM sessions WHERE id = $1",
        session_id,
    )
    assert row["closing_question_asked_at"] is None
    assert row["closing_question_retries"] == 0

    # Still due, so a later poll asks again once the chat is reachable.
    telegram_bot.send_message.side_effect = None
    assert await worker_closing.fire_closing_questions(db_pool, telegram_bot) == [session_id]


async def test_auto_close_stands_even_if_the_notice_cannot_be_sent(db_pool):
    """The close is correct and committed; only the courtesy notice was lost.
    Rolling it back would re-close the session on the very next poll."""
    await db_pool.execute("INSERT INTO chats (chat_id, title) VALUES (1, 'Chat')")
    session_id = await db_pool.fetchval(
        """
        INSERT INTO sessions (chat_id, activity_type, event_date, closing_question_asked_at,
                              closing_question_retries)
        VALUES (1, 'picnic', current_date - 10, now() - interval '3 days', 1) RETURNING id
        """
    )
    telegram_bot = AsyncMock()
    telegram_bot.send_message.side_effect = RuntimeError("Forbidden")

    assert await worker_closing.fire_auto_closes(db_pool, telegram_bot) == [session_id]

    row = await db_pool.fetchrow("SELECT status, closed_reason FROM sessions WHERE id = $1", session_id)
    assert row["status"] == "closed"
    assert row["closed_reason"] == "auto_close_silence"
