async def log_decision(pool, *, chat_id: int, user_id: int | None, raw_text: str | None, stage: str, decision: dict) -> None:
    # `decision` is passed straight through — the pool's jsonb codec
    # (registered in db.pool.create_pool) handles dict <-> jsonb.
    await pool.execute(
        """
        INSERT INTO decision_log (chat_id, user_id, raw_text, stage, decision)
        VALUES ($1, $2, $3, $4, $5)
        """,
        chat_id, user_id, raw_text, stage, decision,
    )
