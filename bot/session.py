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
