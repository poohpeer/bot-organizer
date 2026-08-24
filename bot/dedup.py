async def is_duplicate(pool, update_id: int) -> bool:
    result = await pool.execute(
        "INSERT INTO seen_updates (update_id) VALUES ($1) ON CONFLICT DO NOTHING",
        update_id,
    )
    inserted = result == "INSERT 0 1"
    return not inserted
