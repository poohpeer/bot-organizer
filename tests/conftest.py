import os

os.environ.setdefault("BOT_ORGANIZER_BOT_TOKEN", "123456:test-token")
os.environ.setdefault("GEMINI_API_KEY", "test-key")
os.environ.setdefault("GOOGLE_MAPS_API_KEY", "test-maps-key")

import uuid

import asyncpg
import pytest

import db.pool

TEST_DATABASE_URL = os.environ.get(
    "TEST_DATABASE_URL", "postgresql://postgres:test@localhost:5432/postgres"
)


@pytest.fixture
async def db_pool():
    schema = f"test_{uuid.uuid4().hex[:12]}"
    admin_conn = await asyncpg.connect(TEST_DATABASE_URL)
    try:
        await admin_conn.execute(f"CREATE SCHEMA {schema}")
    finally:
        await admin_conn.close()

    async def _use_schema(conn):
        await conn.execute(f"SET search_path TO {schema}")

    pool = None
    try:
        pool = await db.pool.create_pool(TEST_DATABASE_URL, init=_use_schema)
        await db.pool.init_db(pool)
        yield pool
    finally:
        if pool is not None:
            await pool.close()
        admin_conn = await asyncpg.connect(TEST_DATABASE_URL)
        try:
            await admin_conn.execute(f"DROP SCHEMA {schema} CASCADE")
        finally:
            await admin_conn.close()


@pytest.fixture(autouse=True)
def _no_quiet_hours(monkeypatch):
    """Most tests exercise *whether* a session is due, not what time of day the
    bot is willing to say so. Without this every one of them would pass or fail
    depending on the wall clock when the suite happens to run. The tests that
    are actually about quiet hours put the real window back themselves."""
    import bot.session

    monkeypatch.setattr(bot.session, "QUIET_UNTIL_HOUR", 0)
    monkeypatch.setattr(bot.session, "QUIET_FROM_HOUR", 24)


@pytest.fixture(autouse=True)
def _reset_shared_http_client():
    """Drop the module-global httpx client between tests.

    `bot.tools.external` keeps one client for the whole process, which is right
    in production and wrong across tests: a client created inside one test's
    event loop is unusable from the next one, and the failure surfaces in
    whichever test happens to run second — not in the one that leaked it.
    """
    import bot.tools.external as external

    external._shared_client = None
    yield
    external._shared_client = None
