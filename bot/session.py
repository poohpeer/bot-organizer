import asyncpg


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


async def close_session(pool, session_id: int, reason: str) -> None:
    await pool.execute(
        "UPDATE sessions SET status = 'closed', closed_at = now(), closed_reason = $2 WHERE id = $1",
        session_id, reason,
    )


async def touch_activity(pool, session_id: int) -> None:
    await pool.execute("UPDATE sessions SET last_activity_at = now() WHERE id = $1", session_id)


_NEEDS_CLOSING_QUESTION_SQL = """
SELECT * FROM sessions
WHERE status = 'active'
  AND (
    (event_date IS NOT NULL AND event_date < current_date AND closing_question_asked_at IS NULL)
    OR (event_date IS NULL AND closing_question_asked_at IS NULL
        AND last_activity_at < now() - interval '7 days')
    OR (closing_question_asked_at IS NOT NULL AND closing_question_retries = 0
        AND closing_question_asked_at < now() - interval '2 days')
  )
"""

_NEEDS_AUTO_CLOSE_SQL = """
SELECT * FROM sessions
WHERE status = 'active'
  AND closing_question_asked_at IS NOT NULL
  AND closing_question_retries >= 1
  AND closing_question_asked_at < now() - interval '2 days'
"""


async def sessions_needing_closing_question(pool):
    return await pool.fetch(_NEEDS_CLOSING_QUESTION_SQL)


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


async def sessions_needing_auto_close(pool):
    return await pool.fetch(_NEEDS_AUTO_CLOSE_SQL)


async def record_closing_reply(pool, session_id: int, continued: bool) -> None:
    if not continued:
        await close_session(pool, session_id, reason="closing_question_yes")
        return
    await pool.execute(
        """
        UPDATE sessions SET closing_question_asked_at = NULL, closing_question_retries = 0
        WHERE id = $1
        """,
        session_id,
    )
