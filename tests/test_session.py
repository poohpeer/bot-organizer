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
