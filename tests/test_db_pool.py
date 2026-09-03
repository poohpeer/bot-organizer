import pytest

import db.pool

pytestmark = pytest.mark.skipif(
    not __import__("os").environ.get("TEST_DATABASE_URL"),
    reason="TEST_DATABASE_URL not set — see docs/tasks/0001-group-organizer/EPIC.md",
)

EXPECTED_TABLES = {
    "chat_settings",
    "chats", "sessions", "facts", "participants", "list_items",
    "reminders", "places", "seen_updates", "users",
    "decision_log", "pending_confirmations", "roll_calls",
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


async def test_list_items_has_quantity_category_and_claim_columns(db_pool):
    rows = await db_pool.fetch(
        "SELECT column_name FROM information_schema.columns "
        "WHERE table_schema = current_schema() AND table_name = 'list_items'"
    )
    columns = {r["column_name"] for r in rows}
    assert {"quantity", "category", "claimed_by", "claimed_by_user_id"} <= columns


async def test_list_items_alter_preserves_existing_rows(db_pool):
    """The four S3 columns land via ALTER TABLE on a database that may already
    have rows in it — re-running init_db must not lose them or fail to backfill
    NULLs for the new columns."""
    await db_pool.execute("INSERT INTO chats (chat_id, title) VALUES (1, 'Test chat')")
    row = await db_pool.fetchrow(
        "INSERT INTO sessions (chat_id, activity_type) VALUES (1, 'picnic') RETURNING id"
    )
    await db_pool.execute(
        "INSERT INTO list_items (session_id, name) VALUES ($1, 'tomatoes')", row["id"]
    )

    await db.pool.init_db(db_pool)

    item = await db_pool.fetchrow(
        "SELECT quantity, category, claimed_by, claimed_by_user_id FROM list_items "
        "WHERE session_id = $1 AND name = 'tomatoes'", row["id"]
    )
    assert item is not None
    assert item["quantity"] is None
    assert item["category"] is None
    assert item["claimed_by"] is None
    assert item["claimed_by_user_id"] is None


async def test_init_db_upgrades_a_database_built_by_an_older_version(db_pool):
    """init_db has to run against a database that already has the tables but
    not the newest columns — which is every real deployment, and the one case
    a fresh test database can never reproduce.

    A `CREATE UNIQUE INDEX ... (lower(username))` placed in the CREATE block
    rather than the ALTER block passes every test here and takes production
    down on deploy: `CREATE TABLE IF NOT EXISTS participants` is a no-op
    against the existing table, so the column it indexes does not exist yet
    and the whole schema statement fails. This drops the newest columns to put
    the database back into that shape, then runs init_db over it.
    """
    await db_pool.execute("DROP INDEX IF EXISTS one_username_per_session")
    await db_pool.execute("ALTER TABLE participants DROP COLUMN IF EXISTS username")
    await db_pool.execute("ALTER TABLE reminders DROP COLUMN IF EXISTS deliveries")
    await db_pool.execute("DROP TABLE IF EXISTS roll_calls")

    await db.pool.init_db(db_pool)

    columns = {
        r["column_name"] for r in await db_pool.fetch(
            "SELECT column_name FROM information_schema.columns "
            "WHERE table_schema = current_schema() AND table_name = 'participants'"
        )
    }
    assert "username" in columns
    assert await db_pool.fetchval(
        "SELECT count(*) FROM information_schema.tables "
        "WHERE table_schema = current_schema() AND table_name = 'roll_calls'"
    ) == 1


async def test_init_db_moves_a_bare_handle_out_of_the_display_name(db_pool):
    """Rows written before the username column put "@poohpeer" in
    display_name, which no lookup searches — so the duplicate they are half of
    could never heal. The migration moves the handle into its own column; it
    does not merge anybody, which is the group's call, not a migration's."""
    await db_pool.execute("INSERT INTO chats (chat_id, title) VALUES (-1, 'Chat')")
    session_id = await db_pool.fetchval(
        "INSERT INTO sessions (chat_id, activity_type) VALUES (-1, 'picnic') RETURNING id"
    )
    await db_pool.execute(
        "INSERT INTO participants (session_id, display_name, status) VALUES ($1, $2, $3)",
        session_id, "@poohpeer", "declined",
    )
    await db_pool.execute(
        "INSERT INTO participants (session_id, display_name, status) VALUES ($1, $2, $3)",
        session_id, "Игорёк", "confirmed",
    )

    await db.pool.init_db(db_pool)

    rows = {
        r["display_name"]: r["username"] for r in await db_pool.fetch(
            "SELECT display_name, username FROM participants WHERE session_id = $1",
            session_id,
        )
    }
    assert rows["@poohpeer"] == "poohpeer"
    # A name is never turned into a handle — that would mention whoever really
    # owns it.
    assert rows["Игорёк"] is None
    assert len(rows) == 2, "the migration must not merge or delete anything"


async def test_the_backfill_leaves_a_row_alone_rather_than_colliding(db_pool):
    """A session where the handle is already on another row: taking it here
    would break the unique index and fail the whole migration for everyone."""
    await db_pool.execute("INSERT INTO chats (chat_id, title) VALUES (-2, 'Chat')")
    session_id = await db_pool.fetchval(
        "INSERT INTO sessions (chat_id, activity_type) VALUES (-2, 'picnic') RETURNING id"
    )
    await db_pool.execute(
        "INSERT INTO participants (session_id, display_name, username, status) "
        "VALUES ($1, 'Alex', 'poohpeer', 'confirmed')", session_id
    )
    await db_pool.execute(
        "INSERT INTO participants (session_id, display_name, status) "
        "VALUES ($1, '@poohpeer', 'declined')", session_id
    )

    await db.pool.init_db(db_pool)

    assert await db_pool.fetchval(
        "SELECT username FROM participants WHERE session_id = $1 AND display_name = '@poohpeer'",
        session_id,
    ) is None
