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


# --- regression tests for code-review findings on the closing-question policy ---

async def test_not_yet_reply_stops_the_reask_loop_for_event_date_sessions(db_pool):
    """R4: an explicit 'not yet' must stop further closing questions until
    conditions change. Without a snooze, a past event_date re-qualifies on the
    very next tick and the bot asks forever."""
    await db_pool.execute("INSERT INTO chats (chat_id, title) VALUES (1, 'Chat')")
    row = await session.start_session(db_pool, chat_id=1, activity_type="picnic")
    await db_pool.execute("UPDATE sessions SET event_date = current_date - 1 WHERE id = $1", row["id"])

    assert len(await session.sessions_needing_closing_question(db_pool)) == 1
    await session.mark_closing_question_asked(db_pool, row["id"])
    assert await session.record_closing_reply(db_pool, row["id"], continued=True) is True

    assert await session.sessions_needing_closing_question(db_pool) == []
    assert await session.sessions_needing_auto_close(db_pool) == []


async def test_not_yet_reply_stops_the_reask_loop_for_idle_sessions(db_pool):
    await db_pool.execute("INSERT INTO chats (chat_id, title) VALUES (1, 'Chat')")
    row = await session.start_session(db_pool, chat_id=1, activity_type="trip")
    await db_pool.execute(
        "UPDATE sessions SET last_activity_at = now() - interval '8 days' WHERE id = $1", row["id"]
    )
    await session.mark_closing_question_asked(db_pool, row["id"])

    await session.record_closing_reply(db_pool, row["id"], continued=True)

    assert await session.sessions_needing_closing_question(db_pool) == []


async def test_snooze_expires_and_the_question_becomes_due_again(db_pool):
    await db_pool.execute("INSERT INTO chats (chat_id, title) VALUES (1, 'Chat')")
    row = await session.start_session(db_pool, chat_id=1, activity_type="picnic")
    await db_pool.execute("UPDATE sessions SET event_date = current_date - 1 WHERE id = $1", row["id"])
    await session.mark_closing_question_asked(db_pool, row["id"])
    await session.record_closing_reply(db_pool, row["id"], continued=True)

    await db_pool.execute(
        "UPDATE sessions SET closing_question_snoozed_until = now() - interval '1 minute' WHERE id = $1",
        row["id"],
    )

    assert len(await session.sessions_needing_closing_question(db_pool)) == 1


async def test_close_session_is_idempotent_and_keeps_the_first_reason(db_pool):
    """Auto-close and an explicit stop can race; the second must not overwrite
    the recorded reason or report success (which would post a second summary)."""
    await db_pool.execute("INSERT INTO chats (chat_id, title) VALUES (1, 'Chat')")
    row = await session.start_session(db_pool, chat_id=1, activity_type="bbq")

    assert await session.close_session(db_pool, row["id"], reason="auto_close_silence") is True
    assert await session.close_session(db_pool, row["id"], reason="explicit_stop") is False

    closed = await db_pool.fetchrow("SELECT closed_reason FROM sessions WHERE id = $1", row["id"])
    assert closed["closed_reason"] == "auto_close_silence"


async def test_close_session_rejects_an_unknown_reason(db_pool):
    await db_pool.execute("INSERT INTO chats (chat_id, title) VALUES (1, 'Chat')")
    row = await session.start_session(db_pool, chat_id=1, activity_type="bbq")

    with pytest.raises(ValueError, match="unknown close reason"):
        await session.close_session(db_pool, row["id"], reason="because_i_said_so")


async def test_record_closing_reply_reports_false_for_an_already_closed_session(db_pool):
    """A late 'not yet' arriving after auto-close must not silently mutate a
    closed row — the caller needs to know the session is gone."""
    await db_pool.execute("INSERT INTO chats (chat_id, title) VALUES (1, 'Chat')")
    row = await session.start_session(db_pool, chat_id=1, activity_type="picnic")
    await session.close_session(db_pool, row["id"], reason="auto_close_silence")

    assert await session.record_closing_reply(db_pool, row["id"], continued=True) is False


async def test_claim_for_closing_question_is_atomic_and_marks_in_one_step(db_pool):
    """Two pollers must not both claim the same session and double-post."""
    await db_pool.execute("INSERT INTO chats (chat_id, title) VALUES (1, 'Chat')")
    row = await session.start_session(db_pool, chat_id=1, activity_type="picnic")
    await db_pool.execute("UPDATE sessions SET event_date = current_date - 1 WHERE id = $1", row["id"])

    first = await session.claim_sessions_for_closing_question(db_pool)
    second = await session.claim_sessions_for_closing_question(db_pool)

    assert [r["id"] for r in first] == [row["id"]]
    assert second == []
    marked = await db_pool.fetchrow("SELECT closing_question_asked_at FROM sessions WHERE id = $1", row["id"])
    assert marked["closing_question_asked_at"] is not None


async def test_claim_for_auto_close_closes_and_returns_rows_once(db_pool):
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

    first = await session.claim_sessions_for_auto_close(db_pool)
    second = await session.claim_sessions_for_auto_close(db_pool)

    assert [r["id"] for r in first] == [row["id"]]
    assert second == []
    closed = await db_pool.fetchrow("SELECT status, closed_reason FROM sessions WHERE id = $1", row["id"])
    assert (closed["status"], closed["closed_reason"]) == ("closed", "auto_close_silence")
