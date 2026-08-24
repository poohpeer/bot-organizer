"""End-to-end lifecycle checks for R3/R4.

The per-function tests in test_session.py cover each policy branch in
isolation; these walk a session through the whole arc the way the worker and
router actually will, which is where ordering bugs (re-ask loops, premature
auto-close) show up.
"""

import bot.session as session


async def _age(db_pool, session_id, field, interval):
    await db_pool.execute(
        f"UPDATE sessions SET {field} = {field} - interval '{interval}' WHERE id = $1",
        session_id,
    )


async def test_r4_unanswered_closing_question_ends_in_auto_close(db_pool):
    """Day-after ask -> silence -> one re-ask -> silence -> auto-close, and
    never more than one message per stage."""
    await db_pool.execute("INSERT INTO chats (chat_id, title) VALUES (1, 'Chat')")
    row = await session.start_session(db_pool, chat_id=1, activity_type="picnic")
    session_id = row["id"]
    await db_pool.execute("UPDATE sessions SET event_date = current_date - 1 WHERE id = $1", session_id)

    assert len(await session.claim_sessions_for_closing_question(db_pool)) == 1
    # Same tick again, and any tick before the retry window: stays quiet.
    assert await session.claim_sessions_for_closing_question(db_pool) == []
    assert await session.claim_sessions_for_auto_close(db_pool) == []

    await _age(db_pool, session_id, "closing_question_asked_at", "3 days")
    assert len(await session.claim_sessions_for_closing_question(db_pool)) == 1
    assert await session.claim_sessions_for_auto_close(db_pool) == []

    await _age(db_pool, session_id, "closing_question_asked_at", "3 days")
    closed = await session.claim_sessions_for_auto_close(db_pool)
    assert [r["id"] for r in closed] == [session_id]

    final = await db_pool.fetchrow("SELECT status, closed_reason FROM sessions WHERE id = $1", session_id)
    assert (final["status"], final["closed_reason"]) == ("closed", "auto_close_silence")
    assert await session.get_active_session(db_pool, 1) is None


async def test_r4_not_yet_keeps_the_session_alive_and_quiet(db_pool):
    """'Not yet' must keep the session active AND stop the bot re-asking for
    the whole snooze window."""
    await db_pool.execute("INSERT INTO chats (chat_id, title) VALUES (1, 'Chat')")
    row = await session.start_session(db_pool, chat_id=1, activity_type="trip")
    session_id = row["id"]
    await db_pool.execute("UPDATE sessions SET event_date = current_date - 1 WHERE id = $1", session_id)
    await session.claim_sessions_for_closing_question(db_pool)

    assert await session.record_closing_reply(db_pool, session_id, continued=True) is True

    for elapsed_days in (1, 3, 6):
        await db_pool.execute(
            "UPDATE sessions SET closing_question_snoozed_until = "
            f"now() + interval '{session.SNOOZE_DAYS} days' - interval '{elapsed_days} days' "
            "WHERE id = $1",
            session_id,
        )
        assert await session.sessions_needing_closing_question(db_pool) == []

    assert await session.get_active_session(db_pool, 1) is not None


async def test_r3_ambient_activity_never_closes_a_session(db_pool):
    """R3: only an explicit stop closes a session — chatter never does."""
    await db_pool.execute("INSERT INTO chats (chat_id, title) VALUES (1, 'Chat')")
    row = await session.start_session(db_pool, chat_id=1, activity_type="bbq")

    for _ in range(5):
        await session.touch_activity(db_pool, row["id"])
    assert await session.get_active_session(db_pool, 1) is not None
    assert await session.sessions_needing_closing_question(db_pool) == []

    assert await session.close_session(db_pool, row["id"], reason="explicit_stop") is True
    assert await session.get_active_session(db_pool, 1) is None
