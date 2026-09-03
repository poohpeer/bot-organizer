import os

import asyncpg

import bot.timezones as timezones

# How long a "not yet, we're still going" reply defers the closing question.
# R4 says the bot must not keep asking once someone has answered; without a
# deferral a session whose event_date has passed re-qualifies on the very next
# worker tick and asks forever.
SNOOZE_DAYS = 7

CLOSE_REASONS = frozenset({"explicit_stop", "closing_question_yes", "auto_close_silence"})


class SessionAlreadyActiveError(Exception):
    pass


async def get_active_session(pool, chat_id: int):
    return await pool.fetchrow(
        "SELECT * FROM sessions WHERE chat_id = $1 AND status = 'active'", chat_id
    )


async def start_session(pool, chat_id: int, activity_type: str, *, event_date=None, event_date_raw=None):
    if await get_active_session(pool, chat_id) is not None:
        raise SessionAlreadyActiveError(f"chat {chat_id} already has an active session")
    try:
        return await pool.fetchrow(
            """
            INSERT INTO sessions (chat_id, activity_type, event_date, event_date_raw)
            VALUES ($1, $2, $3, $4)
            RETURNING *
            """,
            chat_id, activity_type, event_date, event_date_raw,
        )
    except asyncpg.UniqueViolationError:
        raise SessionAlreadyActiveError(f"chat {chat_id} already has an active session")


async def retopic(pool, session_id: int, activity_type: str, *, event_date=None, event_date_raw=None):
    """Point an active session at a different event, keeping everything under
    it.

    A group gets one session at a time, so somebody proposing a second event
    is either replacing the first or nothing happens at all. Replacing by
    closing and reopening would throw away the shopping list, the confirmed
    participants and the pending reminders — all of which the group built by
    hand and most of which survive a change of plan.

    A date is only overwritten when a new one was actually stated: switching
    from a picnic to a trip says nothing about when the trip is, and keeping
    yesterday's date would be worse than keeping none.
    """
    row = await pool.fetchrow(
        """
        UPDATE sessions
        SET activity_type = $2,
            event_date = COALESCE($3, event_date),
            event_date_raw = COALESCE($4, event_date_raw),
            last_activity_at = now()
        WHERE id = $1 AND status = 'active'
        RETURNING *
        """,
        session_id, activity_type, event_date, event_date_raw,
    )
    if row is not None:
        # The list and the participants survive a change of plan; a roll call
        # does not. It was asking who was coming to the *old* event, and its
        # summary would name that one.
        await cancel_roll_calls_for_session(pool, session_id)
    return row


async def close_session(pool, session_id: int, reason: str) -> bool:
    """Close an active session. Returns True if this call actually closed it.

    The `status = 'active'` guard makes closing idempotent and race-safe: if
    the worker's auto-close and a user's explicit "that's it, thanks" land in
    the same window, only the first wins, the recorded reason isn't
    overwritten, and the loser gets False so it can skip posting a second
    closing summary.
    """
    if reason not in CLOSE_REASONS:
        raise ValueError(f"unknown close reason {reason!r}; expected one of {sorted(CLOSE_REASONS)}")
    result = await pool.execute(
        """
        UPDATE sessions SET status = 'closed', closed_at = now(), closed_reason = $2
        WHERE id = $1 AND status = 'active'
        """,
        session_id, reason,
    )
    closed = result == "UPDATE 1"
    if closed:
        # A roll call that outlived its session would keep asking the group
        # about an event that is over. Only on a real close: the guard above
        # means a second caller must not cancel a run the winner just started.
        await cancel_roll_calls_for_session(pool, session_id)
    return closed


async def touch_activity(pool, session_id: int) -> None:
    await pool.execute("UPDATE sessions SET last_activity_at = now() WHERE id = $1", session_id)


# Every branch is gated on the snooze: a session someone has said "not yet"
# about is off-limits until the snooze expires, whichever branch would
# otherwise have matched.
# `current_date` is the *server's* date. "The day after the event" has to mean
# the day after in the group's own timezone, or a chat several hours from UTC
# gets asked on the wrong calendar day. chats.timezone is NULL for most chats,
# hence the fallback to the configured default.
_LOCAL_NOW = "(now() AT TIME ZONE COALESCE(c.timezone, $1))"
_LOCAL_DATE = f"{_LOCAL_NOW}::date"

# The bot may only start a conversation during waking hours, local to the group.
# Getting the local day right (above) means the closing question becomes due the
# moment the date rolls over — i.e. around local midnight, which is a rude time
# to message a group. This delays rather than skips: the worker polls every
# minute, so a question that comes due at 00:05 goes out at the start of the
# window instead.
QUIET_UNTIL_HOUR = int(os.environ.get("QUIET_UNTIL_HOUR", "9"))
QUIET_FROM_HOUR = int(os.environ.get("QUIET_FROM_HOUR", "21"))

_WAKING_HOURS = f"EXTRACT(HOUR FROM {_LOCAL_NOW}) >= $2 AND EXTRACT(HOUR FROM {_LOCAL_NOW}) < $3"

# The same predicate, for queries outside this module (worker/reminders.py).
# Exported rather than re-written there so there is one definition of "the
# group is awake" — two would drift, and the second would be the one nobody
# remembered to update. Callers must pass the same three parameters in the
# same positions: default timezone, QUIET_UNTIL_HOUR, QUIET_FROM_HOUR.
WAKING_HOURS_SQL = _WAKING_HOURS

_DUE_FOR_CLOSING_QUESTION = f"""
    status = 'active'
    AND (closing_question_snoozed_until IS NULL OR closing_question_snoozed_until <= now())
    AND (
        (event_date IS NOT NULL AND event_date < {_LOCAL_DATE} AND closing_question_asked_at IS NULL)
        OR (event_date IS NULL AND closing_question_asked_at IS NULL
            AND last_activity_at < now() - interval '7 days')
        OR (closing_question_asked_at IS NOT NULL AND closing_question_retries = 0
            AND closing_question_asked_at < now() - interval '2 days')
    )
    AND ({_WAKING_HOURS})
"""

_DUE_FOR_AUTO_CLOSE = f"""
    status = 'active'
    AND (closing_question_snoozed_until IS NULL OR closing_question_snoozed_until <= now())
    AND closing_question_asked_at IS NOT NULL
    AND closing_question_retries >= 1
    AND closing_question_asked_at < now() - interval '2 days'
    AND ({_WAKING_HOURS})
"""


async def claim_sessions_for_closing_question(pool):
    """Atomically claim every session due for a closing question and return them.

    Claim-and-mark in one statement rather than select-then-mark: two pollers
    (or a tick overlapping a slow send) would otherwise both see the same row
    and post the question twice.

    Each row also carries `prev_asked_at`/`prev_retries`, its state before the
    claim, so a caller whose send fails can put the row back exactly as it was
    via `release_closing_question_claim`. Without that the session stays marked
    as "asked" although nothing was ever sent, and two days later auto-closes a
    session the group was never actually asked about — action without consent,
    which is the opposite of what R4 wants.
    """
    return await pool.fetch(
        f"""
        WITH due AS (
            SELECT id,
                   closing_question_asked_at AS prev_asked_at,
                   closing_question_retries  AS prev_retries
            FROM sessions JOIN chats c USING (chat_id) WHERE {_DUE_FOR_CLOSING_QUESTION} FOR UPDATE SKIP LOCKED
        )
        UPDATE sessions s SET
            closing_question_retries = CASE
                WHEN due.prev_asked_at IS NULL THEN 0
                ELSE due.prev_retries + 1
            END,
            closing_question_asked_at = now()
        FROM due WHERE s.id = due.id
        RETURNING s.*, due.prev_asked_at, due.prev_retries
        """,
        timezones.DEFAULT_TIMEZONE, QUIET_UNTIL_HOUR, QUIET_FROM_HOUR,
    )


async def claim_sessions_for_auto_close(pool):
    """Atomically close every session whose closing question went unanswered
    twice, returning the rows that were closed so the caller can announce it."""
    return await pool.fetch(
        f"""
        UPDATE sessions SET status = 'closed', closed_at = now(),
                            closed_reason = 'auto_close_silence'
        WHERE id IN (
            SELECT id FROM sessions JOIN chats c USING (chat_id)
            WHERE {_DUE_FOR_AUTO_CLOSE} FOR UPDATE SKIP LOCKED
        )
        RETURNING *
        """,
        timezones.DEFAULT_TIMEZONE, QUIET_UNTIL_HOUR, QUIET_FROM_HOUR,
    )


async def release_closing_question_claim(pool, session_id: int, prev_asked_at, prev_retries: int) -> None:
    """Undo a claim whose closing question could not be delivered, restoring the
    exact prior state so the question is asked again on a later poll."""
    await pool.execute(
        """
        UPDATE sessions SET closing_question_asked_at = $2, closing_question_retries = $3
        WHERE id = $1
        """,
        session_id, prev_asked_at, prev_retries,
    )


async def sessions_needing_closing_question(pool):
    """Read-only view of what's due, for tests and diagnostics. The worker uses
    claim_sessions_for_closing_question so it can't double-send."""
    return await pool.fetch(
        f"SELECT s.* FROM sessions s JOIN chats c USING (chat_id) WHERE {_DUE_FOR_CLOSING_QUESTION}",
        timezones.DEFAULT_TIMEZONE, QUIET_UNTIL_HOUR, QUIET_FROM_HOUR,
    )


async def sessions_needing_auto_close(pool):
    """Read-only counterpart to claim_sessions_for_auto_close."""
    return await pool.fetch(
        f"SELECT s.* FROM sessions s JOIN chats c USING (chat_id) WHERE {_DUE_FOR_AUTO_CLOSE}",
        timezones.DEFAULT_TIMEZONE, QUIET_UNTIL_HOUR, QUIET_FROM_HOUR,
    )


async def mark_closing_question_asked(pool, session_id: int) -> None:
    await pool.execute(
        """
        UPDATE sessions SET
            closing_question_retries = CASE
                WHEN closing_question_asked_at IS NULL THEN 0
                ELSE closing_question_retries + 1
            END,
            closing_question_asked_at = now()
        WHERE id = $1
        """,
        session_id,
    )


# --- roll calls -------------------------------------------------------------
#
# Rounds are claimed with the same claim-and-mark-in-one-statement shape the
# closing question uses above, and for the same reason: two pollers, or one
# poller overlapping a slow send, would otherwise both see the same due row
# and post the round twice.
#
# _WAKING_HOURS sits in the *claim*, not around the send. That is what makes
# quiet hours defer a round rather than consume one: at 23:30 the row is not
# selected at all, so rounds_done does not move, and at 09:00 it is simply
# overdue and goes out on the first poll of the morning.

_DUE_FOR_ROLL_CALL = f"""
    status = 'active'
    AND next_at <= now()
    AND ({_WAKING_HOURS})
"""


async def start_roll_call(pool, session_id: int, chat_id: int):
    """Begin a roll call, due immediately.

    Returns None when one is already running for this session — the partial
    unique index decides that, not a read-then-write, so two "спроси всех" a
    second apart cannot both start one and post every round twice.
    """
    return await pool.fetchrow(
        """
        INSERT INTO roll_calls (session_id, chat_id, next_at)
        VALUES ($1, $2, now())
        ON CONFLICT DO NOTHING
        RETURNING *
        """,
        session_id, chat_id,
    )


async def active_roll_call(pool, session_id: int):
    return await pool.fetchrow(
        "SELECT * FROM roll_calls WHERE session_id = $1 AND status = 'active'", session_id
    )


async def claim_due_roll_calls(pool, intervals_minutes):
    """Claim every roll call whose next round is due, and schedule the one
    after it — in one statement.

    One statement, not select-then-update, for the same reason as the closing
    question: the lock a separate SELECT ... FOR UPDATE takes is gone the
    moment that query returns, so two pollers would both see the row as due
    and both post the round.

    `intervals_minutes` is passed in rather than imported so this module stays
    free of bot.roll_call — session.py is storage, and the schedule is a
    policy decision that belongs with the wording. It is indexed in SQL
    (arrays are 1-based, hence the +1) and clamped to its own last element, so
    a row somehow past the last round still gets a sane next_at.

    Each row carries its state before the claim (`prev_rounds_done`,
    `prev_next_at`) so a caller whose send fails can put it back exactly as it
    was. Without that a round that was never delivered still counts as one of
    the three, and the group is asked twice instead of three times.
    """
    return await pool.fetch(
        f"""
        WITH due AS (
            SELECT r.id,
                   r.rounds_done AS prev_rounds_done,
                   r.next_at     AS prev_next_at
            FROM roll_calls r JOIN chats c USING (chat_id)
            WHERE {_DUE_FOR_ROLL_CALL}
            FOR UPDATE SKIP LOCKED
        )
        UPDATE roll_calls r SET
            rounds_done = due.prev_rounds_done + 1,
            next_at = now() + make_interval(
                mins => ($4::int[])[LEAST(GREATEST(due.prev_rounds_done, 0), $5) + 1]
            )
        FROM due WHERE r.id = due.id
        RETURNING r.*, due.prev_rounds_done, due.prev_next_at
        """,
        timezones.DEFAULT_TIMEZONE, QUIET_UNTIL_HOUR, QUIET_FROM_HOUR,
        list(intervals_minutes), len(intervals_minutes) - 1,
    )


async def release_roll_call_claim(pool, roll_call_id: int, prev_rounds_done: int,
                                  prev_next_at) -> None:
    """Undo a claim whose round could not be delivered, restoring the exact
    prior state so it is retried on a later poll."""
    await pool.execute(
        "UPDATE roll_calls SET rounds_done = $2, next_at = $3 WHERE id = $1",
        roll_call_id, prev_rounds_done, prev_next_at,
    )


async def finish_roll_call(pool, roll_call_id: int) -> None:
    await pool.execute(
        "UPDATE roll_calls SET status = 'done' WHERE id = $1 AND status = 'active'",
        roll_call_id,
    )


async def cancel_roll_calls_for_session(pool, session_id: int) -> None:
    """Stop any roll call for a session that is closing or changing event.

    A run that outlived its event would keep asking about a picnic the group
    already went to, or about the wrong event entirely after a retopic.
    """
    await pool.execute(
        "UPDATE roll_calls SET status = 'cancelled' WHERE session_id = $1 AND status = 'active'",
        session_id,
    )


async def roll_calls_due(pool):
    """Read-only view of what's due, for tests and diagnostics. The worker uses
    claim_due_roll_calls so it can't double-send."""
    return await pool.fetch(
        f"""
        SELECT r.* FROM roll_calls r JOIN chats c USING (chat_id)
        WHERE {_DUE_FOR_ROLL_CALL}
        """,
        timezones.DEFAULT_TIMEZONE, QUIET_UNTIL_HOUR, QUIET_FROM_HOUR,
    )


async def record_closing_reply(pool, session_id: int, continued: bool) -> bool:
    """Apply an explicit answer to the closing question.

    Returns True if it applied. False means the session was no longer active —
    e.g. auto-close fired before a late "not yet" arrived — so the caller can
    tell the user the session already closed instead of silently doing nothing.
    """
    if not continued:
        return await close_session(pool, session_id, reason="closing_question_yes")

    result = await pool.execute(
        f"""
        UPDATE sessions SET
            closing_question_asked_at = NULL,
            closing_question_retries = 0,
            closing_question_snoozed_until = now() + interval '{SNOOZE_DAYS} days',
            last_activity_at = now()
        WHERE id = $1 AND status = 'active'
        """,
        session_id,
    )
    return result == "UPDATE 1"
