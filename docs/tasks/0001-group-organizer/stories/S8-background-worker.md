# Story S8: Background worker — reminder delivery + closing-question firing

**Part of:** Group organizer bot (`../EPIC.md`)
**Goal:** An independent process that polls Postgres on a timer and does
everything that has to happen on wall-clock time rather than in response
to an incoming chat message: sending due reminders (rate-limited per
person) and firing the day-after / idle-timeout closing question, so
none of it is lost if the bot process restarts.
**Satisfies:** R4, R11
**Depends on:** S1, S3 (`worker.closing` calls `bot.session`; `worker.reminders`
only touches the `reminders` table directly — no S4 import)
**Parallel-safe with:** S6, S7
**Requirements & global constraints:** see `../EPIC.md`

---

### Task 1: Reminder delivery with per-person rate limiting

**Satisfies:** R11

**Files:**
- Create: `worker/reminders.py`
- Create: `tests/test_worker_reminders.py`

**Interfaces:**
- Consumes: `reminders` table (S1), written by `bot.tools.core.reminder_set`
  (S4).
- Produces: `async worker.reminders.deliver_due_reminders(pool, telegram_bot, *, min_interval_hours: float) -> dict` —
  `{"delivered": [reminder_id, ...], "deferred": [reminder_id, ...]}`.
  Consumed by `worker/main.py` (Task 3).

- [ ] **Step 1: Write the failing test** — create `tests/test_worker_reminders.py`:
  ```python
  from datetime import datetime, timedelta, timezone
  from unittest.mock import AsyncMock

  import worker.reminders as worker_reminders


  async def _session(db_pool, chat_id=1):
      await db_pool.execute("INSERT INTO chats (chat_id, title) VALUES ($1, 'Chat') ON CONFLICT DO NOTHING", chat_id)
      row = await db_pool.fetchrow(
          "INSERT INTO sessions (chat_id, activity_type) VALUES ($1, 'picnic') RETURNING id", chat_id
      )
      return row["id"]


  async def _reminder(db_pool, session_id, *, chat_id=1, target_user_id=None, remind_at=None, status="pending", sent_at=None):
      remind_at = remind_at or (datetime.now(timezone.utc) - timedelta(minutes=1))
      row = await db_pool.fetchrow(
          """
          INSERT INTO reminders (session_id, chat_id, target_user_id, message, remind_at, status, sent_at)
          VALUES ($1, $2, $3, 'Reminder text', $4, $5, $6) RETURNING id
          """,
          session_id, chat_id, target_user_id, remind_at, status, sent_at,
      )
      return row["id"]


  async def test_delivers_due_reminder_and_marks_sent(db_pool):
      session_id = await _session(db_pool)
      reminder_id = await _reminder(db_pool, session_id, target_user_id=111)
      telegram_bot = AsyncMock()

      result = await worker_reminders.deliver_due_reminders(db_pool, telegram_bot, min_interval_hours=6)

      assert result == {"delivered": [reminder_id], "deferred": []}
      telegram_bot.send_message.assert_awaited_once_with(chat_id=111, text="Reminder text")
      row = await db_pool.fetchrow("SELECT status FROM reminders WHERE id = $1", reminder_id)
      assert row["status"] == "sent"


  async def test_ignores_reminders_not_yet_due(db_pool):
      session_id = await _session(db_pool)
      await _reminder(db_pool, session_id, target_user_id=111, remind_at=datetime.now(timezone.utc) + timedelta(hours=1))
      telegram_bot = AsyncMock()

      result = await worker_reminders.deliver_due_reminders(db_pool, telegram_bot, min_interval_hours=6)

      assert result == {"delivered": [], "deferred": []}
      telegram_bot.send_message.assert_not_awaited()


  async def test_defers_when_person_was_reminded_too_recently(db_pool):
      session_id = await _session(db_pool)
      await _reminder(
          db_pool, session_id, target_user_id=111, status="sent",
          sent_at=datetime.now(timezone.utc) - timedelta(hours=1),
      )
      due_id = await _reminder(db_pool, session_id, target_user_id=111)
      telegram_bot = AsyncMock()

      result = await worker_reminders.deliver_due_reminders(db_pool, telegram_bot, min_interval_hours=6)

      assert result == {"delivered": [], "deferred": [due_id]}
      telegram_bot.send_message.assert_not_awaited()
      row = await db_pool.fetchrow("SELECT status FROM reminders WHERE id = $1", due_id)
      assert row["status"] == "pending"  # stays pending, retried next poll


  async def test_group_reminder_uses_chat_id_as_target(db_pool):
      session_id = await _session(db_pool)
      reminder_id = await _reminder(db_pool, session_id, chat_id=1, target_user_id=None)
      telegram_bot = AsyncMock()

      await worker_reminders.deliver_due_reminders(db_pool, telegram_bot, min_interval_hours=6)

      telegram_bot.send_message.assert_awaited_once_with(chat_id=1, text="Reminder text")
  ```

- [ ] **Step 2: Run it, confirm it fails**
  ```bash
  TEST_DATABASE_URL=postgresql://postgres:test@localhost:5432/postgres \
    uv run pytest tests/test_worker_reminders.py -v
  ```
  Expected: `ModuleNotFoundError: No module named 'worker.reminders'`.

- [ ] **Step 3: Implement `worker/reminders.py`**
  ```python
  from datetime import datetime, timedelta, timezone


  async def _due_reminders(pool):
      return await pool.fetch(
          "SELECT * FROM reminders WHERE status = 'pending' AND remind_at <= now() ORDER BY remind_at"
      )


  async def _last_sent_at(pool, *, target_user_id, chat_id):
      if target_user_id is not None:
          return await pool.fetchval(
              "SELECT MAX(sent_at) FROM reminders WHERE target_user_id = $1 AND status = 'sent'",
              target_user_id,
          )
      return await pool.fetchval(
          "SELECT MAX(sent_at) FROM reminders WHERE target_user_id IS NULL AND chat_id = $1 AND status = 'sent'",
          chat_id,
      )


  async def deliver_due_reminders(pool, telegram_bot, *, min_interval_hours: float) -> dict:
      delivered, deferred = [], []
      for r in await _due_reminders(pool):
          last_sent = await _last_sent_at(pool, target_user_id=r["target_user_id"], chat_id=r["chat_id"])
          if last_sent is not None and (datetime.now(timezone.utc) - last_sent) < timedelta(hours=min_interval_hours):
              deferred.append(r["id"])
              continue

          target_chat_id = r["target_user_id"] if r["target_user_id"] is not None else r["chat_id"]
          await telegram_bot.send_message(chat_id=target_chat_id, text=r["message"])
          await pool.execute("UPDATE reminders SET status = 'sent', sent_at = now() WHERE id = $1", r["id"])
          delivered.append(r["id"])

      return {"delivered": delivered, "deferred": deferred}
  ```

- [ ] **Step 4: Run tests, confirm pass**
  ```bash
  TEST_DATABASE_URL=postgresql://postgres:test@localhost:5432/postgres \
    uv run pytest tests/test_worker_reminders.py -v
  ```
  Expected: `4 passed`.

- [ ] **Step 5: Commit**
  ```bash
  git add worker/reminders.py tests/test_worker_reminders.py
  git commit -m "Add reminder delivery with per-person rate limiting"
  ```

---

### Task 2: Closing-question firing and auto-close

**Satisfies:** R4

**Files:**
- Create: `worker/closing.py`
- Create: `tests/test_worker_closing.py`

**Interfaces:**
- Consumes: `bot.session.sessions_needing_closing_question`,
  `bot.session.mark_closing_question_asked`,
  `bot.session.sessions_needing_auto_close`, `bot.session.close_session` (S3).
- Produces: `async worker.closing.fire_closing_questions(pool, telegram_bot) -> list[int]` (session ids asked),
  `async worker.closing.fire_auto_closes(pool, telegram_bot) -> list[int]` (session ids auto-closed).
  Both consumed by `worker/main.py` (Task 3).

- [ ] **Step 1: Write the failing test** — create `tests/test_worker_closing.py`:
  ```python
  from unittest.mock import AsyncMock

  import bot.session as session
  import worker.closing as worker_closing


  async def _overdue_session(db_pool, chat_id=1):
      await db_pool.execute("INSERT INTO chats (chat_id, title) VALUES ($1, 'Chat') ON CONFLICT DO NOTHING", chat_id)
      row = await session.start_session(db_pool, chat_id=chat_id, activity_type="picnic")
      await db_pool.execute("UPDATE sessions SET event_date = current_date - 1 WHERE id = $1", row["id"])
      return row["id"]


  async def test_fire_closing_questions_asks_and_marks(db_pool):
      session_id = await _overdue_session(db_pool)
      telegram_bot = AsyncMock()

      asked = await worker_closing.fire_closing_questions(db_pool, telegram_bot)

      assert asked == [session_id]
      telegram_bot.send_message.assert_awaited_once()
      assert telegram_bot.send_message.await_args.kwargs["chat_id"] == 1
      row = await db_pool.fetchrow("SELECT closing_question_asked_at FROM sessions WHERE id = $1", session_id)
      assert row["closing_question_asked_at"] is not None


  async def test_fire_closing_questions_skips_sessions_not_due(db_pool):
      await db_pool.execute("INSERT INTO chats (chat_id, title) VALUES (1, 'Chat')")
      await session.start_session(db_pool, chat_id=1, activity_type="picnic")
      telegram_bot = AsyncMock()

      asked = await worker_closing.fire_closing_questions(db_pool, telegram_bot)

      assert asked == []
      telegram_bot.send_message.assert_not_awaited()


  async def test_fire_auto_closes_closes_and_notifies(db_pool):
      session_id = await _overdue_session(db_pool)
      await db_pool.execute(
          """
          UPDATE sessions SET closing_question_asked_at = now() - interval '3 days',
                               closing_question_retries = 1
          WHERE id = $1
          """,
          session_id,
      )
      telegram_bot = AsyncMock()

      closed = await worker_closing.fire_auto_closes(db_pool, telegram_bot)

      assert closed == [session_id]
      telegram_bot.send_message.assert_awaited_once()
      row = await db_pool.fetchrow("SELECT status, closed_reason FROM sessions WHERE id = $1", session_id)
      assert row["status"] == "closed"
      assert row["closed_reason"] == "auto_close_silence"


  async def test_fire_auto_closes_skips_sessions_not_due(db_pool):
      session_id = await _overdue_session(db_pool)
      telegram_bot = AsyncMock()

      closed = await worker_closing.fire_auto_closes(db_pool, telegram_bot)

      assert closed == []
      telegram_bot.send_message.assert_not_awaited()
  ```

- [ ] **Step 2: Run it, confirm it fails**
  ```bash
  TEST_DATABASE_URL=postgresql://postgres:test@localhost:5432/postgres \
    uv run pytest tests/test_worker_closing.py -v
  ```
  Expected: `ModuleNotFoundError: No module named 'worker.closing'`.

- [ ] **Step 3: Implement `worker/closing.py`**
  ```python
  import bot.session as session

  _CLOSING_QUESTION_TEXT = "Ну как всё прошло? Я вам ещё нужен или можно закрывать?"
  _AUTO_CLOSE_NOTICE = "Не дождался ответа — закрываю сессию сам."


  async def fire_closing_questions(pool, telegram_bot) -> list[int]:
      due = await session.sessions_needing_closing_question(pool)
      asked = []
      for row in due:
          await telegram_bot.send_message(chat_id=row["chat_id"], text=_CLOSING_QUESTION_TEXT)
          await session.mark_closing_question_asked(pool, row["id"])
          asked.append(row["id"])
      return asked


  async def fire_auto_closes(pool, telegram_bot) -> list[int]:
      due = await session.sessions_needing_auto_close(pool)
      closed = []
      for row in due:
          await session.close_session(pool, row["id"], reason="auto_close_silence")
          await telegram_bot.send_message(chat_id=row["chat_id"], text=_AUTO_CLOSE_NOTICE)
          closed.append(row["id"])
      return closed
  ```

- [ ] **Step 4: Run tests, confirm pass**
  ```bash
  TEST_DATABASE_URL=postgresql://postgres:test@localhost:5432/postgres \
    uv run pytest tests/test_worker_closing.py -v
  ```
  Expected: `4 passed`.

- [ ] **Step 5: Commit**
  ```bash
  git add worker/closing.py tests/test_worker_closing.py
  git commit -m "Add closing-question firing and auto-close to the worker"
  ```

---

### Task 3: Poll loop entrypoint

**Satisfies:** R4, R11

**Files:**
- Create: `worker/main.py`
- Create: `tests/test_worker_main.py`

**Interfaces:**
- Consumes: `worker.reminders.deliver_due_reminders` (Task 1),
  `worker.closing.fire_closing_questions`/`fire_auto_closes` (Task 2),
  `db.pool.create_pool`/`init_db` (S1).
- Produces: `async worker.main.poll_once(pool, telegram_bot, *, min_interval_hours: float) -> None`,
  `worker.main.main()` — the process entrypoint (`worker/main.py` run
  directly), polling every 60 seconds forever.

- [ ] **Step 1: Write the failing test** — create `tests/test_worker_main.py`:
  ```python
  from unittest.mock import AsyncMock, patch

  import worker.main as worker_main


  async def test_poll_once_calls_all_three_worker_steps():
      pool, telegram_bot = object(), object()

      with (
          patch("worker.main.deliver_due_reminders", AsyncMock()) as deliver,
          patch("worker.main.fire_closing_questions", AsyncMock()) as closing_q,
          patch("worker.main.fire_auto_closes", AsyncMock()) as auto_close,
      ):
          await worker_main.poll_once(pool, telegram_bot, min_interval_hours=6)

      deliver.assert_awaited_once_with(pool, telegram_bot, min_interval_hours=6)
      closing_q.assert_awaited_once_with(pool, telegram_bot)
      auto_close.assert_awaited_once_with(pool, telegram_bot)
  ```

- [ ] **Step 2: Run it, confirm it fails**
  ```bash
  uv run pytest tests/test_worker_main.py -v
  ```
  Expected: `ModuleNotFoundError: No module named 'worker.main'`.

- [ ] **Step 3: Implement `worker/main.py`**
  ```python
  import asyncio
  import logging
  import os

  from telegram import Bot

  import db.pool as db_pool_module
  from worker.closing import fire_auto_closes, fire_closing_questions
  from worker.reminders import deliver_due_reminders

  logging.basicConfig(
      format="%(asctime)s %(levelname)s %(name)s: %(message)s", level=logging.INFO
  )
  log = logging.getLogger(__name__)

  POLL_INTERVAL_SECONDS = 60


  async def poll_once(pool, telegram_bot, *, min_interval_hours: float) -> None:
      await deliver_due_reminders(pool, telegram_bot, min_interval_hours=min_interval_hours)
      await fire_closing_questions(pool, telegram_bot)
      await fire_auto_closes(pool, telegram_bot)


  async def main() -> None:
      pool = await db_pool_module.create_pool(os.environ["DATABASE_URL"])
      await db_pool_module.init_db(pool)
      telegram_bot = Bot(token=os.environ["BOT_TOKEN"])
      min_interval_hours = float(os.environ.get("REMINDER_MIN_INTERVAL_HOURS", "6"))

      log.info("Worker started, polling every %ds", POLL_INTERVAL_SECONDS)
      while True:
          try:
              await poll_once(pool, telegram_bot, min_interval_hours=min_interval_hours)
          except Exception:
              log.exception("Poll iteration failed, continuing")
          await asyncio.sleep(POLL_INTERVAL_SECONDS)


  if __name__ == "__main__":
      asyncio.run(main())
  ```

- [ ] **Step 4: Run tests, confirm pass**
  ```bash
  uv run pytest tests/test_worker_main.py -v
  ```
  Expected: `1 passed`.

- [ ] **Step 5: Manual smoke test**
  ```bash
  uv run python -m py_compile worker/main.py worker/reminders.py worker/closing.py
  DATABASE_URL=postgresql://postgres:postgres@localhost:5432/bot_organizer \
    BOT_TOKEN=<real token> uv run python -m worker.main
  ```
  Confirm it logs `Worker started, polling every 60s` and does not crash on
  the first poll iteration against an empty database.

- [ ] **Step 6: Commit**
  ```bash
  git add worker/main.py tests/test_worker_main.py
  git commit -m "Add worker poll-loop entrypoint"
  ```
