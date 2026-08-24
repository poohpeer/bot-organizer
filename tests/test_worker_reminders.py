import asyncio
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

    assert result == {"delivered": [reminder_id], "deferred": [], "failed": []}
    telegram_bot.send_message.assert_awaited_once_with(chat_id=111, text="Reminder text")
    row = await db_pool.fetchrow("SELECT status FROM reminders WHERE id = $1", reminder_id)
    assert row["status"] == "sent"


async def test_ignores_reminders_not_yet_due(db_pool):
    session_id = await _session(db_pool)
    await _reminder(db_pool, session_id, target_user_id=111, remind_at=datetime.now(timezone.utc) + timedelta(hours=1))
    telegram_bot = AsyncMock()

    result = await worker_reminders.deliver_due_reminders(db_pool, telegram_bot, min_interval_hours=6)

    assert result == {"delivered": [], "deferred": [], "failed": []}
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

    assert result == {"delivered": [], "deferred": [due_id], "failed": []}
    telegram_bot.send_message.assert_not_awaited()
    row = await db_pool.fetchrow("SELECT status FROM reminders WHERE id = $1", due_id)
    assert row["status"] == "pending"  # stays pending, retried next poll


async def test_group_reminder_uses_chat_id_as_target(db_pool):
    session_id = await _session(db_pool)
    reminder_id = await _reminder(db_pool, session_id, chat_id=1, target_user_id=None)
    telegram_bot = AsyncMock()

    await worker_reminders.deliver_due_reminders(db_pool, telegram_bot, min_interval_hours=6)

    telegram_bot.send_message.assert_awaited_once_with(chat_id=1, text="Reminder text")


async def test_one_unreachable_recipient_does_not_strand_the_rest(db_pool):
    """Telegram refuses to DM anyone who never started the bot — the normal
    state for most group members. Letting that abort the loop left every
    later reminder undelivered and pending, to be retried forever."""
    session_id = await _session(db_pool)
    ids = []
    for user_id, text in ((111, "первое"), (222, "второе"), (333, "третье")):
        ids.append(await db_pool.fetchval(
            """
            INSERT INTO reminders (session_id, chat_id, target_user_id, message, remind_at)
            VALUES ($1, 1, $2, $3, now() - interval '5 minutes') RETURNING id
            """,
            session_id, user_id, text,
        ))
    telegram_bot = AsyncMock()

    def refuse_the_first(chat_id, text):
        if chat_id == 111:
            raise RuntimeError("Forbidden: bot was blocked by the user")

    telegram_bot.send_message.side_effect = lambda **kw: refuse_the_first(kw["chat_id"], kw["text"])

    result = await worker_reminders.deliver_due_reminders(db_pool, telegram_bot, min_interval_hours=0)

    assert result["delivered"] == ids[1:]
    assert telegram_bot.send_message.await_count == 3
    rows = await db_pool.fetch("SELECT id, status FROM reminders ORDER BY id")
    statuses = {r["id"]: r["status"] for r in rows}
    assert statuses[ids[0]] == "pending"
    assert statuses[ids[1]] == statuses[ids[2]] == "sent"


async def test_a_reminder_that_can_never_be_delivered_stops_being_retried(db_pool):
    """Without a cap the worker re-attempts a doomed send every 60 seconds and
    the queue never drains."""
    session_id = await _session(db_pool)
    reminder_id = await db_pool.fetchval(
        """
        INSERT INTO reminders (session_id, chat_id, target_user_id, message, remind_at)
        VALUES ($1, 1, 111, 'привет', now() - interval '5 minutes') RETURNING id
        """,
        session_id,
    )
    telegram_bot = AsyncMock()
    telegram_bot.send_message.side_effect = RuntimeError("Forbidden")

    for _ in range(worker_reminders.MAX_ATTEMPTS):
        await worker_reminders.deliver_due_reminders(db_pool, telegram_bot, min_interval_hours=0)

    row = await db_pool.fetchrow("SELECT status, attempts FROM reminders WHERE id = $1", reminder_id)
    assert row["status"] == "failed"
    assert row["attempts"] == worker_reminders.MAX_ATTEMPTS

    # A further poll must not touch it again.
    await worker_reminders.deliver_due_reminders(db_pool, telegram_bot, min_interval_hours=0)
    assert telegram_bot.send_message.await_count == worker_reminders.MAX_ATTEMPTS


async def test_two_workers_do_not_deliver_the_same_reminder_twice(db_pool):
    """Both processes saw the same pending row and both sent it — the user got
    the reminder twice."""
    session_id = await _session(db_pool)
    await db_pool.execute(
        """
        INSERT INTO reminders (session_id, chat_id, target_user_id, message, remind_at)
        VALUES ($1, 1, 555, 'напоминание', now() - interval '5 minutes')
        """,
        session_id,
    )
    first, second = AsyncMock(), AsyncMock()

    await asyncio.gather(
        worker_reminders.deliver_due_reminders(db_pool, first, min_interval_hours=0),
        worker_reminders.deliver_due_reminders(db_pool, second, min_interval_hours=0),
    )

    assert first.send_message.await_count + second.send_message.await_count == 1


async def test_group_reminders_are_not_rate_limited(db_pool):
    """The epic scopes REMINDER_MIN_INTERVAL_HOURS to a Telegram *user id*: it
    exists so one person isn't pestered privately. A group reminder is tied to
    a moment ("выезжаем через час"), so deferring it six hours would deliver it
    after the event instead of protecting anyone."""
    session_id = await _session(db_pool)
    for text, minutes in (("выезжаем через час", 40), ("не забудьте паспорта", 5)):
        await db_pool.execute(
            """
            INSERT INTO reminders (session_id, chat_id, target_user_id, message, remind_at)
            VALUES ($1, 1, NULL, $2, now() - ($3 || ' minutes')::interval)
            """,
            session_id, text, str(minutes),
        )
    telegram_bot = AsyncMock()

    result = await worker_reminders.deliver_due_reminders(db_pool, telegram_bot, min_interval_hours=6)

    assert len(result["delivered"]) == 2
    assert result["deferred"] == []
