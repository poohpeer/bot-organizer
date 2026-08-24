async def remember_fact(pool, session_id, key, value) -> dict:
    await pool.execute(
        "INSERT INTO facts (session_id, key, value) VALUES ($1, $2, $3)",
        session_id, key, value,
    )
    return {"status": "ok"}


async def get_facts(pool, session_id, key=None) -> dict:
    rows = await pool.fetch(
        """
        SELECT DISTINCT ON (key) key, value FROM facts
        WHERE session_id = $1 AND ($2::text IS NULL OR key = $2)
        ORDER BY key, created_at DESC
        """,
        session_id, key,
    )
    return {"facts": {r["key"]: r["value"] for r in rows}}


async def propose_confirmation(pool, *, chat_id, session_id, action_type, action_params) -> dict:
    row = await pool.fetchrow(
        """
        INSERT INTO pending_confirmations (chat_id, session_id, action_type, action_params)
        VALUES ($1, $2, $3, $4) RETURNING id
        """,
        chat_id, session_id, action_type, action_params,
    )
    return {
        "status": "pending_confirmation",
        "confirmation_id": row["id"],
        "message_for_user": (
            f"This needs your confirmation before I do it ({action_type}: {action_params}). "
            "Reply yes to confirm or no to cancel."
        ),
    }


async def get_pending_confirmation(pool, chat_id):
    return await pool.fetchrow(
        """
        SELECT * FROM pending_confirmations
        WHERE chat_id = $1 AND status = 'pending' AND proposed_at > now() - interval '1 day'
        ORDER BY proposed_at DESC LIMIT 1
        """,
        chat_id,
    )


async def resolve_confirmation(pool, confirmation_id, *, confirmed: bool):
    return await pool.fetchrow(
        """
        UPDATE pending_confirmations SET status = $2
        WHERE id = $1 RETURNING *
        """,
        confirmation_id, "confirmed" if confirmed else "rejected",
    )


async def execute_confirmed_action(pool, bot, confirmation) -> dict:
    action_type = confirmation["action_type"]
    params = confirmation["action_params"]
    if action_type == "list_remove_item":
        await pool.execute(
            "DELETE FROM list_items WHERE session_id = $1 AND lower(name) = lower($2)",
            confirmation["session_id"], params["name"],
        )
        return {"status": "executed"}
    if action_type == "broadcast_message":
        await bot.send_message(chat_id=confirmation["chat_id"], text=params["text"])
        return {"status": "executed"}
    return {"status": "unknown_action_type"}


async def list_add(pool, session_id, name) -> dict:
    row = await pool.fetchrow(
        "INSERT INTO list_items (session_id, name) VALUES ($1, $2) RETURNING id",
        session_id, name,
    )
    return {"status": "ok", "item_id": row["id"]}


async def list_show(pool, session_id) -> dict:
    rows = await pool.fetch(
        "SELECT name, status FROM list_items WHERE session_id = $1 ORDER BY created_at",
        session_id,
    )
    return {"items": [{"name": r["name"], "status": r["status"]} for r in rows]}


async def list_check_off(pool, session_id, name) -> dict:
    row = await pool.fetchrow(
        """
        UPDATE list_items SET status = 'checked', checked_at = now()
        WHERE session_id = $1 AND lower(name) = lower($2) AND status = 'pending'
        RETURNING id
        """,
        session_id, name,
    )
    return {"status": "ok"} if row else {"status": "not_found"}


async def list_remove_item(pool, session_id, name) -> dict:
    session_row = await pool.fetchrow("SELECT chat_id FROM sessions WHERE id = $1", session_id)
    return await propose_confirmation(
        pool, chat_id=session_row["chat_id"], session_id=session_id,
        action_type="list_remove_item", action_params={"name": name},
    )
