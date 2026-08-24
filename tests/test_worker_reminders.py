from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock

import worker.reminders as worker_reminders


async def _session(db_pool, chat_id=1):
    await db_pool.execute("INSERT INTO chats (chat_id, title) VALUES ($1, 'Chat') ON CONFLICT DO NOTHING", chat_id)
    row = await db_pool.fetchrow(
        "INSERT INTO sessions (chat_id, activity_type) VALUES ($1, 'picnic') RETURNING id", chat_id
    )
    return row["id"]


async def _reminder(db_pool, session_id, *, chat_id=1, target_user_id=None, remind_at=None, status="pending", sent_at=None):
    remind_at = remind_at or (datetime.now(timezone.utc) - timedelta(minutes=1))
    row = await db_pool.fetchrow(
        """
        INSERT INTO reminders (session_id, chat_id, target_user_id, message, remind_at, status, sent_at)
        VALUES ($1, $2, $3, 'Reminder text', $4, $5, $6) RETURNING id
        """,
        session_id, chat_id, target_user_id, remind_at, status, sent_at,
    )
    return row["id"]


async def test_delivers_due_reminder_and_marks_sent(db_pool):
    session_id = await _session(db_pool)
    reminder_id = await _reminder(db_pool, session_id, target_user_id=111)
    telegram_bot = AsyncMock()

    result = await worker_reminders.deliver_due_reminders(db_pool, telegram_bot, min_interval_hours=6)

    assert result == {"delivered": [reminder_id], "deferred": []}
    telegram_bot.send_message.assert_awaited_once_with(chat_id=111, text="Reminder text")
    row = await db_pool.fetchrow("SELECT status FROM reminders WHERE id = $1", reminder_id)
    assert row["status"] == "sent"


async def test_ignores_reminders_not_yet_due(db_pool):
    session_id = await _session(db_pool)
    await _reminder(db_pool, session_id, target_user_id=111, remind_at=datetime.now(timezone.utc) + timedelta(hours=1))
    telegram_bot = AsyncMock()

    result = await worker_reminders.deliver_due_reminders(db_pool, telegram_bot, min_interval_hours=6)

    assert result == {"delivered": [], "deferred": []}
    telegram_bot.send_message.assert_not_awaited()


async def test_defers_when_person_was_reminded_too_recently(db_pool):
    session_id = await _session(db_pool)
    await _reminder(
        db_pool, session_id, target_user_id=111, status="sent",
        sent_at=datetime.now(timezone.utc) - timedelta(hours=1),
    )
    due_id = await _reminder(db_pool, session_id, target_user_id=111)
    telegram_bot = AsyncMock()

    result = await worker_reminders.deliver_due_reminders(db_pool, telegram_bot, min_interval_hours=6)

    assert result == {"delivered": [], "deferred": [due_id]}
    telegram_bot.send_message.assert_not_awaited()
    row = await db_pool.fetchrow("SELECT status FROM reminders WHERE id = $1", due_id)
    assert row["status"] == "pending"  # stays pending, retried next poll


async def test_group_reminder_uses_chat_id_as_target(db_pool):
    session_id = await _session(db_pool)
    reminder_id = await _reminder(db_pool, session_id, chat_id=1, target_user_id=None)
    telegram_bot = AsyncMock()

    await worker_reminders.deliver_due_reminders(db_pool, telegram_bot, min_interval_hours=6)

    telegram_bot.send_message.assert_awaited_once_with(chat_id=1, text="Reminder text")
