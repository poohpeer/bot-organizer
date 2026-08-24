import os

os.environ.setdefault("BOT_TOKEN", "123456:test-token")
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
