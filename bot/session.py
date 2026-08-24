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
    return result == "UPDATE 1"


async def touch_activity(pool, session_id: int) -> None:
    await pool.execute("UPDATE sessions SET last_activity_at = now() WHERE id = $1", session_id)


# Every branch is gated on the snooze: a session someone has said "not yet"
# about is off-limits until the snooze expires, whichever branch would
# otherwise have matched.
# `current_date` is the *server's* date. "The day after the event" has to mean
# the day after in the group's own timezone, or a chat several hours from UTC
# gets asked on the wrong calendar day. chats.timezone is NULL for most chats,
# hence the fallback to the configured default.
_LOCAL_DATE = "(now() AT TIME ZONE COALESCE(c.timezone, $1))::date"

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
"""

_DUE_FOR_AUTO_CLOSE = """
    status = 'active'
    AND (closing_question_snoozed_until IS NULL OR closing_question_snoozed_until <= now())
    AND closing_question_asked_at IS NOT NULL
    AND closing_question_retries >= 1
    AND closing_question_asked_at < now() - interval '2 days'
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
        timezones.DEFAULT_TIMEZONE,
    )


async def claim_sessions_for_auto_close(pool):
    """Atomically close every session whose closing question went unanswered
    twice, returning the rows that were closed so the caller can announce it."""
    return await pool.fetch(
        f"""
        UPDATE sessions SET status = 'closed', closed_at = now(),
                            closed_reason = 'auto_close_silence'
        WHERE id IN (SELECT id FROM sessions WHERE {_DUE_FOR_AUTO_CLOSE} FOR UPDATE SKIP LOCKED)
        RETURNING *
        """
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
        timezones.DEFAULT_TIMEZONE,
    )


async def sessions_needing_auto_close(pool):
    """Read-only counterpart to claim_sessions_for_auto_close."""
    return await pool.fetch(f"SELECT * FROM sessions WHERE {_DUE_FOR_AUTO_CLOSE}")


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
