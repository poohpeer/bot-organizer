import bot.decision_log as decision_log


async def test_log_decision_writes_a_row(db_pool):
    await decision_log.log_decision(
        db_pool,
        chat_id=1, user_id=42, raw_text="let's get tomatoes",
        stage="tool_call",
        decision={"tool": "list_add", "args": {"name": "tomatoes"}},
    )

    row = await db_pool.fetchrow("SELECT * FROM decision_log WHERE chat_id = 1")
    assert row["user_id"] == 42
    assert row["stage"] == "tool_call"
    assert row["decision"]["tool"] == "list_add"


async def test_log_decision_allows_null_user_id(db_pool):
    await decision_log.log_decision(
        db_pool, chat_id=1, user_id=None, raw_text=None,
        stage="relevance_filter", decision={"matched": False},
    )

    row = await db_pool.fetchrow("SELECT * FROM decision_log WHERE chat_id = 1")
    assert row["user_id"] is None
