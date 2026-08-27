import pytest

import db.pool

pytestmark = pytest.mark.skipif(
    not __import__("os").environ.get("TEST_DATABASE_URL"),
    reason="TEST_DATABASE_URL not set — see docs/tasks/0001-group-organizer/EPIC.md",
)

EXPECTED_TABLES = {
    "chats", "sessions", "facts", "participants", "list_items",
    "reminders", "places", "seen_updates",
    "decision_log", "pending_confirmations",
}


async def test_init_db_creates_all_tables(db_pool):
    rows = await db_pool.fetch(
        "SELECT table_name FROM information_schema.tables "
        "WHERE table_schema = current_schema()"
    )
    assert {r["table_name"] for r in rows} == EXPECTED_TABLES


async def test_only_one_active_session_per_chat(db_pool):
    await db_pool.execute(
        "INSERT INTO chats (chat_id, title) VALUES (1, 'Test chat')"
    )
    await db_pool.execute(
        "INSERT INTO sessions (chat_id, activity_type, status) "
        "VALUES (1, 'picnic', 'active')"
    )
    with pytest.raises(Exception, match="duplicate key|unique"):
        await db_pool.execute(
            "INSERT INTO sessions (chat_id, activity_type, status) "
            "VALUES (1, 'birthday', 'active')"
        )


async def test_jsonb_columns_round_trip_as_python_dicts(db_pool):
    await db_pool.execute(
        "INSERT INTO decision_log (chat_id, stage, decision) VALUES ($1, $2, $3)",
        1, "test", {"tool": "list_add", "args": {"name": "tomatoes"}},
    )

    row = await db_pool.fetchrow("SELECT decision FROM decision_log WHERE chat_id = 1")

    assert row["decision"] == {"tool": "list_add", "args": {"name": "tomatoes"}}


async def test_reminders_table_has_repeat_columns(db_pool):
    rows = await db_pool.fetch(
        "SELECT column_name FROM information_schema.columns "
        "WHERE table_schema = current_schema() AND table_name = 'reminders'"
    )
    columns = {r["column_name"] for r in rows}
    assert {"repeat_every_minutes", "repeat_until"} <= columns


async def test_init_db_twice_still_succeeds(db_pool):
    """_ALTERS_SQL runs on every process startup against a database that may
    already have every column and constraint from a previous run — the second
    call must be a no-op, not an error."""
    await db.pool.init_db(db_pool)
