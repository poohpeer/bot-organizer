# Story S3: Session lifecycle — state machine, closing-question policy, dedup, decision log

**Part of:** Group organizer bot (`../EPIC.md`)
**Goal:** Own everything about a session's life: starting, stopping,
the day-after/idle-timeout closing-question policy, Telegram update
dedup, and the decision log — all as plain functions over the `sessions`
table from S1, callable by S9 (message router) and S8 (background worker)
without either of them reimplementing this policy.
**Satisfies:** R3, R4, R10
**Depends on:** S1
**Parallel-safe with:** S4, S5
**Requirements & global constraints:** see `../EPIC.md`

---

### Task 1: Session start/stop core

**Satisfies:** R3

**Files:**
- Create: `bot/session.py`
- Create: `tests/test_session.py`

**Interfaces:**
- Consumes: `db.pool` schema from S1 (`sessions` table, `db_pool` fixture).
- Produces:
  - `class bot.session.SessionAlreadyActiveError(Exception)`
  - `async bot.session.get_active_session(pool, chat_id: int) -> asyncpg.Record | None`
  - `async bot.session.start_session(pool, chat_id: int, activity_type: str, *, event_date: date | None = None, event_date_raw: str | None = None) -> asyncpg.Record` —
    raises `SessionAlreadyActiveError` if the chat already has an active
    session.
  - `async bot.session.close_session(pool, session_id: int, reason: str) -> None`
  - `async bot.session.touch_activity(pool, session_id: int) -> None`

- [ ] **Step 1: Write the failing test** — create `tests/test_session.py`:
  ```python
  import pytest

  import bot.session as session


  async def test_start_session_creates_active_session(db_pool):
      await db_pool.execute("INSERT INTO chats (chat_id, title) VALUES (1, 'Chat')")

      row = await session.start_session(db_pool, chat_id=1, activity_type="picnic")

      assert row["chat_id"] == 1
      assert row["activity_type"] == "picnic"
      assert row["status"] == "active"


  async def test_start_session_rejects_second_active_session(db_pool):
      await db_pool.execute("INSERT INTO chats (chat_id, title) VALUES (1, 'Chat')")
      await session.start_session(db_pool, chat_id=1, activity_type="picnic")

      with pytest.raises(session.SessionAlreadyActiveError):
          await session.start_session(db_pool, chat_id=1, activity_type="birthday")


  async def test_get_active_session_returns_none_when_dormant(db_pool):
      await db_pool.execute("INSERT INTO chats (chat_id, title) VALUES (1, 'Chat')")

      assert await session.get_active_session(db_pool, chat_id=1) is None


  async def test_close_session_marks_closed_with_reason(db_pool):
      await db_pool.execute("INSERT INTO chats (chat_id, title) VALUES (1, 'Chat')")
      row = await session.start_session(db_pool, chat_id=1, activity_type="picnic")

      await session.close_session(db_pool, row["id"], reason="explicit_stop")

      assert await session.get_active_session(db_pool, chat_id=1) is None
      closed = await db_pool.fetchrow("SELECT * FROM sessions WHERE id = $1", row["id"])
      assert closed["status"] == "closed"
      assert closed["closed_reason"] == "explicit_stop"
      assert closed["closed_at"] is not None


  async def test_touch_activity_updates_last_activity_at(db_pool):
      await db_pool.execute("INSERT INTO chats (chat_id, title) VALUES (1, 'Chat')")
      row = await session.start_session(db_pool, chat_id=1, activity_type="picnic")
      before = row["last_activity_at"]

      await db_pool.execute("UPDATE sessions SET last_activity_at = now() - interval '1 hour' WHERE id = $1", row["id"])
      await session.touch_activity(db_pool, row["id"])

      updated = await db_pool.fetchrow("SELECT last_activity_at FROM sessions WHERE id = $1", row["id"])
      assert updated["last_activity_at"] > before
  ```

- [ ] **Step 2: Run it, confirm it fails**
  ```bash
  TEST_DATABASE_URL=postgresql://postgres:test@localhost:5432/postgres \
    uv run pytest tests/test_session.py -v
  ```
  Expected: `ModuleNotFoundError: No module named 'bot.session'`.

- [ ] **Step 3: Implement `bot/session.py`**
  ```python
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
  ```

- [ ] **Step 4: Run tests, confirm pass**
  ```bash
  TEST_DATABASE_URL=postgresql://postgres:test@localhost:5432/postgres \
    uv run pytest tests/test_session.py -v
  ```
  Expected: `5 passed`.

- [ ] **Step 5: Commit**
  ```bash
  git add bot/session.py tests/test_session.py
  git commit -m "Add session start/stop core (bot/session.py)"
  ```

---

### Task 2: Closing-question policy

**Satisfies:** R4

**Files:**
- Modify: `bot/session.py`
- Modify: `tests/test_session.py`

**Interfaces:**
- Consumes: `sessions` table (S1), Task 1 functions in this story.
- Produces:
  - `async bot.session.sessions_needing_closing_question(pool) -> list[asyncpg.Record]` —
    consumed by S8.
  - `async bot.session.mark_closing_question_asked(pool, session_id: int) -> None` —
    consumed by S8.
  - `async bot.session.sessions_needing_auto_close(pool) -> list[asyncpg.Record]` —
    consumed by S8.
  - `async bot.session.record_closing_reply(pool, session_id: int, continued: bool) -> None` —
    consumed by S9.

- [ ] **Step 1: Write the failing test** — append to `tests/test_session.py`:
  ```python
  async def test_needs_closing_question_when_event_date_passed(db_pool):
      await db_pool.execute("INSERT INTO chats (chat_id, title) VALUES (1, 'Chat')")
      row = await session.start_session(db_pool, chat_id=1, activity_type="picnic")
      await db_pool.execute(
          "UPDATE sessions SET event_date = current_date - 1 WHERE id = $1", row["id"]
      )

      due = await session.sessions_needing_closing_question(db_pool)

      assert [r["id"] for r in due] == [row["id"]]


  async def test_needs_closing_question_when_idle_a_week_and_no_date(db_pool):
      await db_pool.execute("INSERT INTO chats (chat_id, title) VALUES (1, 'Chat')")
      row = await session.start_session(db_pool, chat_id=1, activity_type="picnic")
      await db_pool.execute(
          "UPDATE sessions SET last_activity_at = now() - interval '8 days' WHERE id = $1", row["id"]
      )

      due = await session.sessions_needing_closing_question(db_pool)

      assert [r["id"] for r in due] == [row["id"]]


  async def test_not_due_yet_when_recently_active_and_no_date(db_pool):
      await db_pool.execute("INSERT INTO chats (chat_id, title) VALUES (1, 'Chat')")
      await session.start_session(db_pool, chat_id=1, activity_type="picnic")

      assert await session.sessions_needing_closing_question(db_pool) == []


  async def test_mark_closing_question_asked_sets_timestamp_then_increments_retry(db_pool):
      await db_pool.execute("INSERT INTO chats (chat_id, title) VALUES (1, 'Chat')")
      row = await session.start_session(db_pool, chat_id=1, activity_type="picnic")

      await session.mark_closing_question_asked(db_pool, row["id"])
      first = await db_pool.fetchrow("SELECT * FROM sessions WHERE id = $1", row["id"])
      assert first["closing_question_asked_at"] is not None
      assert first["closing_question_retries"] == 0

      await session.mark_closing_question_asked(db_pool, row["id"])
      second = await db_pool.fetchrow("SELECT * FROM sessions WHERE id = $1", row["id"])
      assert second["closing_question_retries"] == 1


  async def test_needs_auto_close_after_unanswered_retry(db_pool):
      await db_pool.execute("INSERT INTO chats (chat_id, title) VALUES (1, 'Chat')")
      row = await session.start_session(db_pool, chat_id=1, activity_type="picnic")
      await db_pool.execute(
          """
          UPDATE sessions SET closing_question_asked_at = now() - interval '3 days',
                               closing_question_retries = 1
          WHERE id = $1
          """,
          row["id"],
      )

      due = await session.sessions_needing_auto_close(db_pool)

      assert [r["id"] for r in due] == [row["id"]]


  async def test_record_closing_reply_yes_closes_session(db_pool):
      await db_pool.execute("INSERT INTO chats (chat_id, title) VALUES (1, 'Chat')")
      row = await session.start_session(db_pool, chat_id=1, activity_type="picnic")

      await session.record_closing_reply(db_pool, row["id"], continued=False)

      assert await session.get_active_session(db_pool, chat_id=1) is None


  async def test_record_closing_reply_not_yet_resets_the_question(db_pool):
      await db_pool.execute("INSERT INTO chats (chat_id, title) VALUES (1, 'Chat')")
      row = await session.start_session(db_pool, chat_id=1, activity_type="picnic")
      await session.mark_closing_question_asked(db_pool, row["id"])

      await session.record_closing_reply(db_pool, row["id"], continued=True)

      updated = await db_pool.fetchrow("SELECT * FROM sessions WHERE id = $1", row["id"])
      assert updated["status"] == "active"
      assert updated["closing_question_asked_at"] is None
      assert updated["closing_question_retries"] == 0
  ```

- [ ] **Step 2: Run it, confirm it fails**
  ```bash
  TEST_DATABASE_URL=postgresql://postgres:test@localhost:5432/postgres \
    uv run pytest tests/test_session.py -v
  ```
  Expected: `AttributeError: module 'bot.session' has no attribute 'sessions_needing_closing_question'`.

- [ ] **Step 3: Append to `bot/session.py`**
  ```python
  _NEEDS_CLOSING_QUESTION_SQL = """
  SELECT * FROM sessions
  WHERE status = 'active'
    AND (
      (event_date IS NOT NULL AND event_date < current_date AND closing_question_asked_at IS NULL)
      OR (event_date IS NULL AND closing_question_asked_at IS NULL
          AND last_activity_at < now() - interval '7 days')
      OR (closing_question_asked_at IS NOT NULL AND closing_question_retries = 0
          AND closing_question_asked_at < now() - interval '2 days')
    )
  """

  _NEEDS_AUTO_CLOSE_SQL = """
  SELECT * FROM sessions
  WHERE status = 'active'
    AND closing_question_asked_at IS NOT NULL
    AND closing_question_retries >= 1
    AND closing_question_asked_at < now() - interval '2 days'
  """


  async def sessions_needing_closing_question(pool):
      return await pool.fetch(_NEEDS_CLOSING_QUESTION_SQL)


  async def mark_closing_question_asked(pool, session_id: int) -> None:
      await pool.execute(
          """
          UPDATE sessions SET
              closing_question_retries = CASE
                  WHEN closing_question_asked_at IS NULL THEN 0
                  ELSE closing_question_retries + 1
              END,
              closing_question_asked_at = now()
          WHERE id = $1
          """,
          session_id,
      )


  async def sessions_needing_auto_close(pool):
      return await pool.fetch(_NEEDS_AUTO_CLOSE_SQL)


  async def record_closing_reply(pool, session_id: int, continued: bool) -> None:
      if not continued:
          await close_session(pool, session_id, reason="closing_question_yes")
          return
      await pool.execute(
          """
          UPDATE sessions SET closing_question_asked_at = NULL, closing_question_retries = 0
          WHERE id = $1
          """,
          session_id,
      )
  ```

- [ ] **Step 4: Run tests, confirm pass**
  ```bash
  TEST_DATABASE_URL=postgresql://postgres:test@localhost:5432/postgres \
    uv run pytest tests/test_session.py -v
  ```
  Expected: `12 passed`.

- [ ] **Step 5: Commit**
  ```bash
  git add bot/session.py tests/test_session.py
  git commit -m "Add closing-question policy (day-after, idle timeout, retry, auto-close)"
  ```

---

### Task 3: Update dedup + decision log

**Satisfies:** R10

**Files:**
- Create: `bot/dedup.py`
- Create: `bot/decision_log.py`
- Create: `tests/test_dedup.py`
- Create: `tests/test_decision_log.py`

**Interfaces:**
- Consumes: `seen_updates`, `decision_log` tables (S1).
- Produces:
  - `async bot.dedup.is_duplicate(pool, update_id: int) -> bool` — consumed
    by S9, before any handler with side effects runs.
  - `async bot.decision_log.log_decision(pool, *, chat_id: int, user_id: int | None, raw_text: str | None, stage: str, decision: dict) -> None` —
    consumed by S9, S7.

- [ ] **Step 1: Write the failing tests** — create `tests/test_dedup.py`:
  ```python
  import bot.dedup as dedup


  async def test_first_sighting_is_not_duplicate(db_pool):
      assert await dedup.is_duplicate(db_pool, 12345) is False


  async def test_second_sighting_is_duplicate(db_pool):
      await dedup.is_duplicate(db_pool, 12345)

      assert await dedup.is_duplicate(db_pool, 12345) is True
  ```

  Create `tests/test_decision_log.py`:
  ```python
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
          stage="proactive_filter", decision={"matched": False},
      )

      row = await db_pool.fetchrow("SELECT * FROM decision_log WHERE chat_id = 1")
      assert row["user_id"] is None
  ```

- [ ] **Step 2: Run it, confirm it fails**
  ```bash
  TEST_DATABASE_URL=postgresql://postgres:test@localhost:5432/postgres \
    uv run pytest tests/test_dedup.py tests/test_decision_log.py -v
  ```
  Expected: both modules missing (`ModuleNotFoundError`).

- [ ] **Step 3: Implement `bot/dedup.py`**
  ```python
  async def is_duplicate(pool, update_id: int) -> bool:
      result = await pool.execute(
          "INSERT INTO seen_updates (update_id) VALUES ($1) ON CONFLICT DO NOTHING",
          update_id,
      )
      inserted = result == "INSERT 0 1"
      return not inserted
  ```

  Implement `bot/decision_log.py`:
  ```python
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
  ```

- [ ] **Step 4: Run tests, confirm pass**
  ```bash
  TEST_DATABASE_URL=postgresql://postgres:test@localhost:5432/postgres \
    uv run pytest tests/test_dedup.py tests/test_decision_log.py -v
  ```
  Expected: `4 passed`.

- [ ] **Step 5: Commit**
  ```bash
  git add bot/dedup.py bot/decision_log.py tests/test_dedup.py tests/test_decision_log.py
  git commit -m "Add Telegram update dedup and decision log"
  ```
