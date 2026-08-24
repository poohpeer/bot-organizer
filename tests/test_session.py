import pytest

import bot.session as session


async def test_start_session_creates_active_session(db_pool):
    await db_pool.execute("INSERT INTO chats (chat_id, title) VALUES (1, 'Chat')")

    row = await session.start_session(db_pool, chat_id=1, activity_type="picnic")

    assert row["chat_id"] == 1
    assert row["activity_type"] == "picnic"
    assert row["status"] == "active"


async def test_start_session_rejects_second_active_session(db_pool):
    await db_pool.execute("INSERT INTO chats (chat_id, title) VALUES (1, 'Chat')")
    await session.start_session(db_pool, chat_id=1, activity_type="picnic")

    with pytest.raises(session.SessionAlreadyActiveError):
        await session.start_session(db_pool, chat_id=1, activity_type="birthday")


async def test_get_active_session_returns_none_when_dormant(db_pool):
    await db_pool.execute("INSERT INTO chats (chat_id, title) VALUES (1, 'Chat')")

    assert await session.get_active_session(db_pool, chat_id=1) is None


async def test_close_session_marks_closed_with_reason(db_pool):
    await db_pool.execute("INSERT INTO chats (chat_id, title) VALUES (1, 'Chat')")
    row = await session.start_session(db_pool, chat_id=1, activity_type="picnic")

    await session.close_session(db_pool, row["id"], reason="explicit_stop")

    assert await session.get_active_session(db_pool, chat_id=1) is None
    closed = await db_pool.fetchrow("SELECT * FROM sessions WHERE id = $1", row["id"])
    assert closed["status"] == "closed"
    assert closed["closed_reason"] == "explicit_stop"
    assert closed["closed_at"] is not None


async def test_touch_activity_updates_last_activity_at(db_pool):
    await db_pool.execute("INSERT INTO chats (chat_id, title) VALUES (1, 'Chat')")
    row = await session.start_session(db_pool, chat_id=1, activity_type="picnic")
    before = row["last_activity_at"]

    await db_pool.execute("UPDATE sessions SET last_activity_at = now() - interval '1 hour' WHERE id = $1", row["id"])
    await session.touch_activity(db_pool, row["id"])

    updated = await db_pool.fetchrow("SELECT last_activity_at FROM sessions WHERE id = $1", row["id"])
    assert updated["last_activity_at"] > before


async def test_needs_closing_question_when_event_date_passed(db_pool):
    await db_pool.execute("INSERT INTO chats (chat_id, title) VALUES (1, 'Chat')")
    row = await session.start_session(db_pool, chat_id=1, activity_type="picnic")
    await db_pool.execute(
        "UPDATE sessions SET event_date = current_date - 1 WHERE id = $1", row["id"]
    )

    due = await session.sessions_needing_closing_question(db_pool)

    assert [r["id"] for r in due] == [row["id"]]


async def test_needs_closing_question_when_idle_a_week_and_no_date(db_pool):
    await db_pool.execute("INSERT INTO chats (chat_id, title) VALUES (1, 'Chat')")
    row = await session.start_session(db_pool, chat_id=1, activity_type="picnic")
    await db_pool.execute(
        "UPDATE sessions SET last_activity_at = now() - interval '8 days' WHERE id = $1", row["id"]
    )

    due = await session.sessions_needing_closing_question(db_pool)

    assert [r["id"] for r in due] == [row["id"]]


async def test_not_due_yet_when_recently_active_and_no_date(db_pool):
    await db_pool.execute("INSERT INTO chats (chat_id, title) VALUES (1, 'Chat')")
    await session.start_session(db_pool, chat_id=1, activity_type="picnic")

    assert await session.sessions_needing_closing_question(db_pool) == []


async def test_mark_closing_question_asked_sets_timestamp_then_increments_retry(db_pool):
    await db_pool.execute("INSERT INTO chats (chat_id, title) VALUES (1, 'Chat')")
    row = await session.start_session(db_pool, chat_id=1, activity_type="picnic")

    await session.mark_closing_question_asked(db_pool, row["id"])
    first = await db_pool.fetchrow("SELECT * FROM sessions WHERE id = $1", row["id"])
    assert first["closing_question_asked_at"] is not None
    assert first["closing_question_retries"] == 0

    await session.mark_closing_question_asked(db_pool, row["id"])
    second = await db_pool.fetchrow("SELECT * FROM sessions WHERE id = $1", row["id"])
    assert second["closing_question_retries"] == 1


async def test_needs_auto_close_after_unanswered_retry(db_pool):
    await db_pool.execute("INSERT INTO chats (chat_id, title) VALUES (1, 'Chat')")
    row = await session.start_session(db_pool, chat_id=1, activity_type="picnic")
    await db_pool.execute(
        """
        UPDATE sessions SET closing_question_asked_at = now() - interval '3 days',
                             closing_question_retries = 1
        WHERE id = $1
        """,
        row["id"],
    )

    due = await session.sessions_needing_auto_close(db_pool)

    assert [r["id"] for r in due] == [row["id"]]


async def test_record_closing_reply_yes_closes_session(db_pool):
    await db_pool.execute("INSERT INTO chats (chat_id, title) VALUES (1, 'Chat')")
    row = await session.start_session(db_pool, chat_id=1, activity_type="picnic")

    await session.record_closing_reply(db_pool, row["id"], continued=False)

    assert await session.get_active_session(db_pool, chat_id=1) is None


async def test_record_closing_reply_not_yet_resets_the_question(db_pool):
    await db_pool.execute("INSERT INTO chats (chat_id, title) VALUES (1, 'Chat')")
    row = await session.start_session(db_pool, chat_id=1, activity_type="picnic")
    await session.mark_closing_question_asked(db_pool, row["id"])

    await session.record_closing_reply(db_pool, row["id"], continued=True)

    updated = await db_pool.fetchrow("SELECT * FROM sessions WHERE id = $1", row["id"])
    assert updated["status"] == "active"
    assert updated["closing_question_asked_at"] is None
    assert updated["closing_question_retries"] == 0
