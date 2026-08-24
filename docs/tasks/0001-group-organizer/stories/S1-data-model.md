# Story S1: Data model & persistence

**Part of:** Group organizer bot (`../EPIC.md`)
**Goal:** Bootstrap the Python project and stand up the full Postgres
schema (via `asyncpg`, no ORM) that every later story reads and writes.
**Satisfies:** R1, R2, R4, R5, R8, R9, R10, R11
**Depends on:** none
**Parallel-safe with:** S2
**Requirements & global constraints:** see `../EPIC.md`

---

### Task 1: Project bootstrap

**Satisfies:** (infra — enables all requirements)

**Files:**
- Create: `pyproject.toml`
- Create: `.env.example`
- Create: `bot/__init__.py`, `bot/tools/__init__.py`, `bot/ai/__init__.py`
- Create: `db/__init__.py`
- Create: `worker/__init__.py`
- Create: `tests/__init__.py`, `tests/conftest.py`

**Interfaces:**
- Consumes: nothing (first task in the repo).
- Produces: the `uv`-managed project skeleton; `tests/conftest.py` sets
  `BOT_TOKEN`, `GEMINI_API_KEY`, `GOOGLE_MAPS_API_KEY` env defaults that
  every later test file relies on (same pattern the sibling
  `general-telegram-bot` project uses).

- [ ] **Step 1: Create `pyproject.toml`**
  ```toml
  [project]
  name = "bot-organizer"
  version = "0.1.0"
  requires-python = ">=3.12"
  dependencies = [
      "python-telegram-bot>=22.8",
      "google-genai>=0.5",
      "asyncpg>=0.29",
      "httpx>=0.27",
      "python-dotenv>=1.0",
  ]

  [dependency-groups]
  dev = [
      "pytest>=8.0",
      "pytest-asyncio>=0.24",
  ]

  [tool.pytest.ini_options]
  asyncio_mode = "auto"

  [build-system]
  requires = ["hatchling"]
  build-backend = "hatchling.build"

  [tool.hatch.build.targets.wheel]
  packages = ["bot", "db", "worker"]
  ```

- [ ] **Step 2: Create `.env.example`**
  ```
  BOT_TOKEN=
  GEMINI_API_KEY=
  GOOGLE_MAPS_API_KEY=
  DATABASE_URL=postgresql://postgres:postgres@localhost:5432/bot_organizer
  REMINDER_MIN_INTERVAL_HOURS=6
  ```

- [ ] **Step 3: Create package directories and `tests/conftest.py`**
  ```bash
  mkdir -p bot/tools bot/ai db worker tests
  touch bot/__init__.py bot/tools/__init__.py bot/ai/__init__.py \
        db/__init__.py worker/__init__.py tests/__init__.py
  ```
  `tests/conftest.py`:
  ```python
  import os

  os.environ.setdefault("BOT_TOKEN", "123456:test-token")
  os.environ.setdefault("GEMINI_API_KEY", "test-key")
  os.environ.setdefault("GOOGLE_MAPS_API_KEY", "test-maps-key")
  ```

- [ ] **Step 4: Install and confirm the project imports**
  ```bash
  uv sync
  uv run python -c "import bot, db, worker; print('ok')"
  ```
  Expected output: `ok`.

- [ ] **Step 5: Commit**
  ```bash
  git add pyproject.toml .env.example bot db worker tests uv.lock
  git commit -m "Bootstrap project skeleton (pyproject.toml, package layout, test env defaults)"
  ```

---

### Task 2: Postgres schema & connection pool

**Satisfies:** R1, R2, R4, R5, R8, R9, R10, R11

**Files:**
- Create: `db/pool.py`
- Modify: `tests/conftest.py` (add the `db_pool` fixture)
- Create: `tests/test_db_pool.py`

**Interfaces:**
- Consumes: `pyproject.toml` dependencies from Task 1.
- Produces:
  - `async db.pool.create_pool(dsn: str, *, init=None) -> asyncpg.Pool`
  - `async db.pool.init_db(pool: asyncpg.Pool) -> None` — applies the full
    schema idempotently (`CREATE TABLE IF NOT EXISTS` / `CREATE INDEX IF
    NOT EXISTS`).
  - The `db_pool` pytest fixture (in `tests/conftest.py`) — an
    `asyncpg.Pool` scoped to a fresh, uniquely-named Postgres schema with
    `init_db()` already applied, dropped on teardown. Every later story's
    DB-touching tests use this fixture.
  - The tables: `chats`, `sessions`, `facts`, `participants`,
    `list_items`, `reminders`, `places`, `proactive_suggestions`,
    `seen_updates`, `decision_log`, `pending_confirmations`.

- [ ] **Step 1: Write the failing test** — create `tests/test_db_pool.py`:
  ```python
  import pytest

  pytestmark = pytest.mark.skipif(
      not __import__("os").environ.get("TEST_DATABASE_URL"),
      reason="TEST_DATABASE_URL not set — see docs/tasks/0001-group-organizer/EPIC.md",
  )

  EXPECTED_TABLES = {
      "chats", "sessions", "facts", "participants", "list_items",
      "reminders", "places", "proactive_suggestions", "seen_updates",
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
  ```

- [ ] **Step 2: Run it, confirm it fails**
  ```bash
  TEST_DATABASE_URL=postgresql://postgres:test@localhost:5432/postgres \
    uv run pytest tests/test_db_pool.py -v
  ```
  Expected: collection error / failure — `db.pool` has no attribute
  `create_pool` (module doesn't exist yet), and the `db_pool` fixture
  doesn't exist yet either.

- [ ] **Step 3: Implement `db/pool.py`**
  ```python
  import json

  import asyncpg

  _SCHEMA_SQL = """
  CREATE TABLE IF NOT EXISTS chats (
      chat_id    BIGINT PRIMARY KEY,
      title      TEXT,
      created_at TIMESTAMPTZ NOT NULL DEFAULT now()
  );

  CREATE TABLE IF NOT EXISTS sessions (
      id                          BIGSERIAL PRIMARY KEY,
      chat_id                     BIGINT NOT NULL REFERENCES chats(chat_id),
      activity_type               TEXT NOT NULL,
      status                      TEXT NOT NULL DEFAULT 'active'
                                       CHECK (status IN ('active', 'closed')),
      event_date                  DATE,
      event_date_raw              TEXT,
      started_at                  TIMESTAMPTZ NOT NULL DEFAULT now(),
      closed_at                   TIMESTAMPTZ,
      closed_reason               TEXT
                                       CHECK (closed_reason IN (
                                           'explicit_stop', 'closing_question_yes',
                                           'auto_close_silence'
                                       )),
      last_activity_at            TIMESTAMPTZ NOT NULL DEFAULT now(),
      closing_question_asked_at   TIMESTAMPTZ,
      closing_question_retries    INT NOT NULL DEFAULT 0
  );

  CREATE UNIQUE INDEX IF NOT EXISTS one_active_session_per_chat
      ON sessions (chat_id) WHERE status = 'active';

  CREATE TABLE IF NOT EXISTS facts (
      id          BIGSERIAL PRIMARY KEY,
      session_id  BIGINT NOT NULL REFERENCES sessions(id),
      key         TEXT NOT NULL,
      value       TEXT NOT NULL,
      created_at  TIMESTAMPTZ NOT NULL DEFAULT now()
  );
  CREATE INDEX IF NOT EXISTS idx_facts_session_key ON facts (session_id, key);

  CREATE TABLE IF NOT EXISTS participants (
      id            BIGSERIAL PRIMARY KEY,
      session_id    BIGINT NOT NULL REFERENCES sessions(id),
      user_id       BIGINT,
      display_name  TEXT NOT NULL,
      status        TEXT NOT NULL DEFAULT 'unknown'
                         CHECK (status IN ('unknown', 'confirmed', 'declined')),
      responded_at  TIMESTAMPTZ,
      created_at    TIMESTAMPTZ NOT NULL DEFAULT now()
  );
  CREATE INDEX IF NOT EXISTS idx_participants_session ON participants (session_id);

  CREATE TABLE IF NOT EXISTS list_items (
      id          BIGSERIAL PRIMARY KEY,
      session_id  BIGINT NOT NULL REFERENCES sessions(id),
      name        TEXT NOT NULL,
      status      TEXT NOT NULL DEFAULT 'pending'
                       CHECK (status IN ('pending', 'checked')),
      added_by    BIGINT,
      created_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
      checked_at  TIMESTAMPTZ
  );
  CREATE INDEX IF NOT EXISTS idx_list_items_session ON list_items (session_id);

  CREATE TABLE IF NOT EXISTS reminders (
      id              BIGSERIAL PRIMARY KEY,
      session_id      BIGINT NOT NULL REFERENCES sessions(id),
      chat_id         BIGINT NOT NULL,
      target_user_id  BIGINT,
      message         TEXT NOT NULL,
      remind_at       TIMESTAMPTZ NOT NULL,
      status          TEXT NOT NULL DEFAULT 'pending'
                           CHECK (status IN ('pending', 'sent', 'cancelled')),
      created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
      sent_at         TIMESTAMPTZ
  );
  CREATE INDEX IF NOT EXISTS idx_reminders_due ON reminders (status, remind_at);

  CREATE TABLE IF NOT EXISTS places (
      id           BIGSERIAL PRIMARY KEY,
      session_id   BIGINT NOT NULL REFERENCES sessions(id),
      name         TEXT NOT NULL,
      address      TEXT,
      lat          DOUBLE PRECISION NOT NULL,
      lon          DOUBLE PRECISION NOT NULL,
      resolved_at  TIMESTAMPTZ NOT NULL DEFAULT now()
  );
  CREATE INDEX IF NOT EXISTS idx_places_session ON places (session_id);

  CREATE TABLE IF NOT EXISTS proactive_suggestions (
      id            BIGSERIAL PRIMARY KEY,
      chat_id       BIGINT NOT NULL,
      topic_key     TEXT NOT NULL,
      suggested_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
      response      TEXT CHECK (response IN ('accepted', 'declined', 'ignored'))
  );
  CREATE INDEX IF NOT EXISTS idx_proactive_chat_time
      ON proactive_suggestions (chat_id, suggested_at);
  CREATE INDEX IF NOT EXISTS idx_proactive_topic
      ON proactive_suggestions (chat_id, topic_key, suggested_at);

  CREATE TABLE IF NOT EXISTS seen_updates (
      update_id  BIGINT PRIMARY KEY,
      seen_at    TIMESTAMPTZ NOT NULL DEFAULT now()
  );

  CREATE TABLE IF NOT EXISTS decision_log (
      id          BIGSERIAL PRIMARY KEY,
      chat_id     BIGINT NOT NULL,
      user_id     BIGINT,
      raw_text    TEXT,
      stage       TEXT NOT NULL,
      decision    JSONB NOT NULL,
      created_at  TIMESTAMPTZ NOT NULL DEFAULT now()
  );
  CREATE INDEX IF NOT EXISTS idx_decision_log_chat ON decision_log (chat_id, created_at);

  CREATE TABLE IF NOT EXISTS pending_confirmations (
      id              BIGSERIAL PRIMARY KEY,
      chat_id         BIGINT NOT NULL,
      session_id      BIGINT REFERENCES sessions(id),
      action_type     TEXT NOT NULL,
      action_params   JSONB NOT NULL,
      proposed_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
      status          TEXT NOT NULL DEFAULT 'pending'
                           CHECK (status IN ('pending', 'confirmed', 'rejected', 'expired'))
  );
  CREATE INDEX IF NOT EXISTS idx_pending_confirmations_chat
      ON pending_confirmations (chat_id, status);
  """


  async def create_pool(dsn: str, *, init=None) -> asyncpg.Pool:
      async def _init_connection(conn):
          # jsonb columns (decision_log.decision, pending_confirmations.action_params)
          # round-trip as plain Python dicts, not raw JSON strings.
          await conn.set_type_codec(
              "jsonb", encoder=json.dumps, decoder=json.loads, schema="pg_catalog"
          )
          if init is not None:
              await init(conn)

      return await asyncpg.create_pool(dsn, init=_init_connection)


  async def init_db(pool: asyncpg.Pool) -> None:
      async with pool.acquire() as conn:
          await conn.execute(_SCHEMA_SQL)
  ```

  Add the `db_pool` fixture to `tests/conftest.py` (appended to the file
  from Task 1):
  ```python
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
      await admin_conn.execute(f"CREATE SCHEMA {schema}")
      await admin_conn.close()

      async def _use_schema(conn):
          await conn.execute(f"SET search_path TO {schema}")

      pool = await db.pool.create_pool(TEST_DATABASE_URL, init=_use_schema)
      await db.pool.init_db(pool)
      yield pool
      await pool.close()

      admin_conn = await asyncpg.connect(TEST_DATABASE_URL)
      await admin_conn.execute(f"DROP SCHEMA {schema} CASCADE")
      await admin_conn.close()
  ```

- [ ] **Step 4: Run tests, confirm pass**
  ```bash
  TEST_DATABASE_URL=postgresql://postgres:test@localhost:5432/postgres \
    uv run pytest tests/test_db_pool.py -v
  ```
  Expected: `2 passed`.

- [ ] **Step 5: Commit**
  ```bash
  git add db/pool.py tests/conftest.py tests/test_db_pool.py
  git commit -m "Add Postgres schema and connection pool (db/pool.py)"
  ```
