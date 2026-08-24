# Story S4: Core tools — facts, lists, participants, reminders, confirmation gate

**Part of:** Group organizer bot (`../EPIC.md`)
**Goal:** Implement the DB-backed tool functions the model calls during
an active session for everything that isn't an external lookup, matching
the declarations in `bot/tools/schema.py` (S2) exactly, including the
destructive-action confirmation gate.
**Satisfies:** R1, R2, R6, R10, R11
**Depends on:** S1, S2
**Parallel-safe with:** S3, S5
**Requirements & global constraints:** see `../EPIC.md`

---

## Implementation notes (added during S4, after code review)

`bot/tools/core.py` diverges from the task briefs below where the briefs'
code had real defects. The briefs are kept for provenance; the code is the
source of truth. Each item was reproduced against a real database first.

1. **Participant identity (R2).** The briefs matched on `user_id` OR name in
   mutually exclusive branches, so someone named in the group ("Masha is
   coming") and later replying in DM became **two rows** — `get_participants`
   reported conflicting statuses and the nudge DMed someone who had already
   confirmed. Now: match by `user_id`, fall back to name, backfill the
   `user_id` onto the existing row.
2. **Nudge failure isolation (R2).** Telegram raises `Forbidden` for anyone
   who has never started a private chat with the bot — the *normal* state for
   most group members. The briefs let that propagate, aborting the run,
   losing the record of who was already reached, and re-DMing them on retry.
   Each send is now isolated and the result carries a `failed_to_reach`
   bucket.
3. **Gated actions could execute twice (R10).** `resolve_confirmation` had no
   `status = 'pending'` guard, so two "yes" messages both resolved the same
   row and the destructive action ran twice. Guarded, and
   `execute_confirmed_action` now refuses anything not in `confirmed` state.
4. **Cross-chat writes.** `reminder_set` and `broadcast_message` took
   `chat_id` as a *model-supplied* argument, so a hallucinated value would
   file the confirmation against another chat and post the text there.
   `chat_id` is now derived from `session_id` and removed from the tool
   declarations; `reminder_cancel` is likewise scoped by `session_id` so a
   stale `reminder_id` can't cancel another chat's reminder.
5. **Smaller correctness fixes.** `list_check_off` distinguishes
   `already_checked` from `not_found` (R1: two people both saying "I got the
   cucumbers" shouldn't be told cucumbers aren't listed); the confirmed
   delete removes exactly one row and reports `not_found` if nothing matched;
   tools return `unknown_session` instead of raising `TypeError` on a bad
   `session_id`; `reminder_set` returns `bad_datetime` rather than raising;
   and the confirmation prompt reads as a sentence instead of interpolating a
   raw params dict (it is relayed verbatim into the chat).

Known and deliberately deferred:

- **Timezone.** `reminder_set` treats a naive ISO-8601 timestamp as UTC. The
  model will usually render "remind us at 9am" as naive local wall-clock, so
  in a UTC+3 group the reminder fires 3 hours late. Same root cause as S3's
  deferred `event_date` timezone issue: there is no per-chat timezone in the
  schema. Both should be fixed together by adding one.
- **Expired confirmations.** `get_pending_confirmation` hides rows older than
  a day, but nothing writes the `'expired'` status the schema defines, so
  stale rows sit as `'pending'` forever. Harmless today (the gate filters by
  age) but a sweeper should set the status once anything queries by status
  alone.
- **Inflected check-off.** Matching is exact on `lower(name)`. Russian is
  heavily inflected ("помидоры" added vs "помидорок" spoken), so the model
  must normalize the `name` argument. See the S4 test report for how this
  behaves against the live model.


---

### Task 1: Facts

**Satisfies:** R6

**Files:**
- Create: `bot/tools/core.py`
- Create: `tests/test_tools_core.py`

**Interfaces:**
- Consumes: `facts` table (S1).
- Produces: `async bot.tools.core.remember_fact(pool, session_id, key, value) -> dict`,
  `async bot.tools.core.get_facts(pool, session_id, key=None) -> dict`.

- [ ] **Step 1: Write the failing test** — create `tests/test_tools_core.py`:
  ```python
  import bot.tools.core as core


  async def _new_session(db_pool, chat_id=1):
      await db_pool.execute("INSERT INTO chats (chat_id, title) VALUES ($1, 'Chat')", chat_id)
      row = await db_pool.fetchrow(
          "INSERT INTO sessions (chat_id, activity_type) VALUES ($1, 'picnic') RETURNING id",
          chat_id,
      )
      return row["id"]


  async def test_remember_and_get_fact(db_pool):
      session_id = await _new_session(db_pool)

      await core.remember_fact(db_pool, session_id, "destination", "Hanania meadow")
      facts = await core.get_facts(db_pool, session_id, key="destination")

      assert facts["facts"]["destination"] == "Hanania meadow"


  async def test_get_facts_returns_latest_value_when_updated(db_pool):
      session_id = await _new_session(db_pool)

      await core.remember_fact(db_pool, session_id, "destination", "First place")
      await core.remember_fact(db_pool, session_id, "destination", "Second place")
      facts = await core.get_facts(db_pool, session_id, key="destination")

      assert facts["facts"]["destination"] == "Second place"


  async def test_get_facts_without_key_returns_all_latest(db_pool):
      session_id = await _new_session(db_pool)

      await core.remember_fact(db_pool, session_id, "destination", "Hanania meadow")
      await core.remember_fact(db_pool, session_id, "headcount", "12")
      facts = await core.get_facts(db_pool, session_id)

      assert facts["facts"] == {"destination": "Hanania meadow", "headcount": "12"}


  async def test_get_facts_missing_key_returns_empty(db_pool):
      session_id = await _new_session(db_pool)

      facts = await core.get_facts(db_pool, session_id, key="nope")

      assert facts["facts"] == {}
  ```

- [ ] **Step 2: Run it, confirm it fails**
  ```bash
  TEST_DATABASE_URL=postgresql://postgres:test@localhost:5432/postgres \
    uv run pytest tests/test_tools_core.py -v
  ```
  Expected: `ModuleNotFoundError: No module named 'bot.tools.core'`.

- [ ] **Step 3: Implement `bot/tools/core.py`**
  ```python
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
  ```

- [ ] **Step 4: Run tests, confirm pass**
  ```bash
  TEST_DATABASE_URL=postgresql://postgres:test@localhost:5432/postgres \
    uv run pytest tests/test_tools_core.py -v
  ```
  Expected: `4 passed`.

- [ ] **Step 5: Commit**
  ```bash
  git add bot/tools/core.py tests/test_tools_core.py
  git commit -m "Add remember_fact/get_facts tools"
  ```

---

### Task 2: Destructive-action confirmation gate

**Satisfies:** R10

**Files:**
- Modify: `bot/tools/core.py`
- Modify: `tests/test_tools_core.py`

**Interfaces:**
- Consumes: `pending_confirmations` table (S1).
- Produces:
  - `async bot.tools.core.propose_confirmation(pool, *, chat_id, session_id, action_type, action_params) -> dict`
  - `async bot.tools.core.get_pending_confirmation(pool, chat_id) -> asyncpg.Record | None` —
    consumed by S9.
  - `async bot.tools.core.resolve_confirmation(pool, confirmation_id, *, confirmed: bool) -> asyncpg.Record` —
    consumed by S9.
  - `async bot.tools.core.execute_confirmed_action(pool, bot, confirmation) -> dict` —
    consumed by S9. Dispatches on `action_type`: `list_remove_item`,
    `broadcast_message`.

- [ ] **Step 1: Write the failing test** — append to `tests/test_tools_core.py`:
  ```python
  from unittest.mock import AsyncMock


  async def test_propose_confirmation_creates_pending_row(db_pool):
      session_id = await _new_session(db_pool)

      result = await core.propose_confirmation(
          db_pool, chat_id=1, session_id=session_id,
          action_type="list_remove_item", action_params={"name": "tomatoes"},
      )

      assert result["status"] == "pending_confirmation"
      row = await db_pool.fetchrow(
          "SELECT * FROM pending_confirmations WHERE id = $1", result["confirmation_id"]
      )
      assert row["status"] == "pending"
      assert row["action_params"]["name"] == "tomatoes"


  async def test_get_pending_confirmation_returns_most_recent_pending(db_pool):
      session_id = await _new_session(db_pool)
      await core.propose_confirmation(
          db_pool, chat_id=1, session_id=session_id,
          action_type="list_remove_item", action_params={"name": "old"},
      )
      second = await core.propose_confirmation(
          db_pool, chat_id=1, session_id=session_id,
          action_type="list_remove_item", action_params={"name": "new"},
      )

      found = await core.get_pending_confirmation(db_pool, chat_id=1)

      assert found["id"] == second["confirmation_id"]


  async def test_resolve_confirmation_confirmed(db_pool):
      session_id = await _new_session(db_pool)
      proposed = await core.propose_confirmation(
          db_pool, chat_id=1, session_id=session_id,
          action_type="list_remove_item", action_params={"name": "tomatoes"},
      )

      resolved = await core.resolve_confirmation(db_pool, proposed["confirmation_id"], confirmed=True)

      assert resolved["status"] == "confirmed"


  async def test_execute_confirmed_action_removes_list_item(db_pool):
      session_id = await _new_session(db_pool)
      await db_pool.execute(
          "INSERT INTO list_items (session_id, name) VALUES ($1, 'tomatoes')", session_id
      )
      proposed = await core.propose_confirmation(
          db_pool, chat_id=1, session_id=session_id,
          action_type="list_remove_item", action_params={"name": "tomatoes"},
      )
      confirmation = await core.resolve_confirmation(db_pool, proposed["confirmation_id"], confirmed=True)

      result = await core.execute_confirmed_action(db_pool, AsyncMock(), confirmation)

      assert result["status"] == "executed"
      remaining = await db_pool.fetch("SELECT * FROM list_items WHERE session_id = $1", session_id)
      assert remaining == []


  async def test_execute_confirmed_action_sends_broadcast(db_pool):
      session_id = await _new_session(db_pool)
      proposed = await core.propose_confirmation(
          db_pool, chat_id=1, session_id=session_id,
          action_type="broadcast_message", action_params={"text": "Reminder: bring meat"},
      )
      confirmation = await core.resolve_confirmation(db_pool, proposed["confirmation_id"], confirmed=True)
      bot = AsyncMock()

      result = await core.execute_confirmed_action(db_pool, bot, confirmation)

      assert result["status"] == "executed"
      bot.send_message.assert_awaited_once_with(chat_id=1, text="Reminder: bring meat")
  ```

- [ ] **Step 2: Run it, confirm it fails**
  ```bash
  TEST_DATABASE_URL=postgresql://postgres:test@localhost:5432/postgres \
    uv run pytest tests/test_tools_core.py -v
  ```
  Expected: `AttributeError: module 'bot.tools.core' has no attribute 'propose_confirmation'`.

- [ ] **Step 3: Append to `bot/tools/core.py`**
  ```python
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
  ```

- [ ] **Step 4: Run tests, confirm pass**
  ```bash
  TEST_DATABASE_URL=postgresql://postgres:test@localhost:5432/postgres \
    uv run pytest tests/test_tools_core.py -v
  ```
  Expected: `9 passed`.

- [ ] **Step 5: Commit**
  ```bash
  git add bot/tools/core.py tests/test_tools_core.py
  git commit -m "Add destructive-action confirmation gate"
  ```

---

### Task 3: List operations

**Satisfies:** R1, R10

**Files:**
- Modify: `bot/tools/core.py`
- Modify: `tests/test_tools_core.py`

**Interfaces:**
- Consumes: `list_items` table (S1), `propose_confirmation` (Task 2).
- Produces: `async bot.tools.core.list_add(pool, session_id, name) -> dict`,
  `async bot.tools.core.list_show(pool, session_id) -> dict`,
  `async bot.tools.core.list_check_off(pool, session_id, name) -> dict`,
  `async bot.tools.core.list_remove_item(pool, session_id, name) -> dict`
  (gated — returns a pending-confirmation result, never deletes directly).

- [ ] **Step 1: Write the failing test** — append to `tests/test_tools_core.py`:
  ```python
  async def test_list_add_and_show(db_pool):
      session_id = await _new_session(db_pool)

      await core.list_add(db_pool, session_id, "tomatoes")
      await core.list_add(db_pool, session_id, "cucumbers")
      shown = await core.list_show(db_pool, session_id)

      names = {i["name"] for i in shown["items"]}
      assert names == {"tomatoes", "cucumbers"}
      assert all(i["status"] == "pending" for i in shown["items"])


  async def test_list_check_off_matches_by_name_case_insensitively(db_pool):
      session_id = await _new_session(db_pool)
      await core.list_add(db_pool, session_id, "Cucumbers")

      result = await core.list_check_off(db_pool, session_id, "cucumbers")

      assert result["status"] == "ok"
      shown = await core.list_show(db_pool, session_id)
      assert shown["items"][0]["status"] == "checked"


  async def test_list_check_off_missing_item(db_pool):
      session_id = await _new_session(db_pool)

      result = await core.list_check_off(db_pool, session_id, "nonexistent")

      assert result["status"] == "not_found"


  async def test_list_remove_item_is_gated_not_immediate(db_pool):
      session_id = await _new_session(db_pool)
      await core.list_add(db_pool, session_id, "tomatoes")

      result = await core.list_remove_item(db_pool, session_id, "tomatoes")

      assert result["status"] == "pending_confirmation"
      shown = await core.list_show(db_pool, session_id)
      assert len(shown["items"]) == 1  # not actually removed yet
  ```

- [ ] **Step 2: Run it, confirm it fails**
  ```bash
  TEST_DATABASE_URL=postgresql://postgres:test@localhost:5432/postgres \
    uv run pytest tests/test_tools_core.py -v
  ```
  Expected: `AttributeError: module 'bot.tools.core' has no attribute 'list_add'`.

- [ ] **Step 3: Append to `bot/tools/core.py`**
  ```python
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
  ```

- [ ] **Step 4: Run tests, confirm pass**
  ```bash
  TEST_DATABASE_URL=postgresql://postgres:test@localhost:5432/postgres \
    uv run pytest tests/test_tools_core.py -v
  ```
  Expected: `13 passed`.

- [ ] **Step 5: Commit**
  ```bash
  git add bot/tools/core.py tests/test_tools_core.py
  git commit -m "Add list_add/list_show/list_check_off/list_remove_item tools"
  ```

---

### Task 4: Participants

**Satisfies:** R2

**Files:**
- Modify: `bot/tools/core.py`
- Modify: `tests/test_tools_core.py`

**Interfaces:**
- Consumes: `participants` table (S1), a Telegram `Bot`-like object with
  `async send_message(chat_id, text)` (the real `telegram.Bot` in
  production; an `AsyncMock` in tests).
- Produces: `async bot.tools.core.set_participant(pool, session_id, display_name, status, user_id=None) -> dict`,
  `async bot.tools.core.get_participants(pool, session_id) -> dict`,
  `async bot.tools.core.nudge_unconfirmed_participants(pool, telegram_bot, session_id) -> dict`.

- [ ] **Step 1: Write the failing test** — append to `tests/test_tools_core.py`:
  ```python
  async def test_set_participant_inserts_then_updates(db_pool):
      session_id = await _new_session(db_pool)

      await core.set_participant(db_pool, session_id, "Sasha", "unknown", user_id=111)
      await core.set_participant(db_pool, session_id, "Sasha", "confirmed", user_id=111)

      participants = await core.get_participants(db_pool, session_id)
      assert len(participants["participants"]) == 1
      assert participants["participants"][0]["status"] == "confirmed"


  async def test_set_participant_without_user_id_matches_by_name(db_pool):
      session_id = await _new_session(db_pool)

      await core.set_participant(db_pool, session_id, "Masha", "unknown")
      await core.set_participant(db_pool, session_id, "masha", "declined")

      participants = await core.get_participants(db_pool, session_id)
      assert len(participants["participants"]) == 1
      assert participants["participants"][0]["status"] == "declined"


  async def test_nudge_unconfirmed_dms_only_those_with_known_user_id(db_pool):
      from unittest.mock import AsyncMock

      session_id = await _new_session(db_pool)
      await core.set_participant(db_pool, session_id, "Sasha", "unknown", user_id=111)
      await core.set_participant(db_pool, session_id, "NoTelegram", "unknown")
      await core.set_participant(db_pool, session_id, "Masha", "confirmed", user_id=222)

      telegram_bot = AsyncMock()
      result = await core.nudge_unconfirmed_participants(db_pool, telegram_bot, session_id)

      assert result["nudged"] == ["Sasha"]
      assert result["skipped_no_user_id"] == ["NoTelegram"]
      telegram_bot.send_message.assert_awaited_once()
      assert telegram_bot.send_message.await_args.kwargs["chat_id"] == 111
  ```

- [ ] **Step 2: Run it, confirm it fails**
  ```bash
  TEST_DATABASE_URL=postgresql://postgres:test@localhost:5432/postgres \
    uv run pytest tests/test_tools_core.py -v
  ```
  Expected: `AttributeError: module 'bot.tools.core' has no attribute 'set_participant'`.

- [ ] **Step 3: Append to `bot/tools/core.py`**
  ```python
  async def set_participant(pool, session_id, display_name, status, user_id=None) -> dict:
      if user_id is not None:
          result = await pool.execute(
              """
              UPDATE participants SET status = $3, display_name = $4, responded_at = now()
              WHERE session_id = $1 AND user_id = $2
              """,
              session_id, user_id, status, display_name,
          )
      else:
          result = await pool.execute(
              """
              UPDATE participants SET status = $3, responded_at = now()
              WHERE session_id = $1 AND user_id IS NULL AND lower(display_name) = lower($2)
              """,
              session_id, display_name, status,
          )
      if result == "UPDATE 0":
          await pool.execute(
              """
              INSERT INTO participants (session_id, user_id, display_name, status, responded_at)
              VALUES ($1, $2, $3, $4, now())
              """,
              session_id, user_id, display_name, status,
          )
      return {"status": "ok"}


  async def get_participants(pool, session_id) -> dict:
      rows = await pool.fetch(
          "SELECT user_id, display_name, status FROM participants WHERE session_id = $1",
          session_id,
      )
      return {"participants": [
          {"user_id": r["user_id"], "display_name": r["display_name"], "status": r["status"]}
          for r in rows
      ]}


  async def nudge_unconfirmed_participants(pool, telegram_bot, session_id) -> dict:
      session_row = await pool.fetchrow("SELECT activity_type FROM sessions WHERE id = $1", session_id)
      rows = await pool.fetch(
          "SELECT user_id, display_name FROM participants WHERE session_id = $1 AND status = 'unknown'",
          session_id,
      )
      nudged, skipped = [], []
      for r in rows:
          if r["user_id"] is None:
              skipped.append(r["display_name"])
              continue
          await telegram_bot.send_message(
              chat_id=r["user_id"],
              text=f"Hey {r['display_name']}, are you in for the {session_row['activity_type']}?",
          )
          nudged.append(r["display_name"])
      return {"nudged": nudged, "skipped_no_user_id": skipped}
  ```

- [ ] **Step 4: Run tests, confirm pass**
  ```bash
  TEST_DATABASE_URL=postgresql://postgres:test@localhost:5432/postgres \
    uv run pytest tests/test_tools_core.py -v
  ```
  Expected: `16 passed`.

- [ ] **Step 5: Commit**
  ```bash
  git add bot/tools/core.py tests/test_tools_core.py
  git commit -m "Add participant confirmation tracking and private nudge tool"
  ```

---

### Task 5: Reminders, gated broadcast, and the assembled registry

**Satisfies:** R11, R10

**Files:**
- Modify: `bot/tools/core.py`
- Modify: `tests/test_tools_core.py`

**Interfaces:**
- Consumes: `reminders` table (S1), `propose_confirmation` (Task 2).
- Produces: `async bot.tools.core.reminder_set(pool, session_id, chat_id, message, remind_at, target_user_id=None) -> dict`,
  `async bot.tools.core.reminder_cancel(pool, reminder_id) -> dict`,
  `async bot.tools.core.broadcast_message(pool, session_id, chat_id, text) -> dict` (gated),
  `bot.tools.core.build_core_registry(pool, telegram_bot) -> dict[str, Callable]` —
  consumed by S9 to assemble the full tool registry passed to
  `run_tool_loop`. `reminders` rows are consumed by S8 (the worker).

- [ ] **Step 1: Write the failing test** — append to `tests/test_tools_core.py`:
  ```python
  async def test_reminder_set_persists_to_queue(db_pool):
      session_id = await _new_session(db_pool)

      result = await core.reminder_set(
          db_pool, session_id, chat_id=1, message="Bring the grill",
          remind_at="2026-09-01T09:00:00",
      )

      assert result["status"] == "ok"
      row = await db_pool.fetchrow("SELECT * FROM reminders WHERE id = $1", result["reminder_id"])
      assert row["status"] == "pending"
      assert row["message"] == "Bring the grill"


  async def test_reminder_cancel_marks_cancelled(db_pool):
      session_id = await _new_session(db_pool)
      created = await core.reminder_set(
          db_pool, session_id, chat_id=1, message="x", remind_at="2026-09-01T09:00:00"
      )

      result = await core.reminder_cancel(db_pool, created["reminder_id"])

      assert result["status"] == "ok"
      row = await db_pool.fetchrow("SELECT status FROM reminders WHERE id = $1", created["reminder_id"])
      assert row["status"] == "cancelled"


  async def test_reminder_cancel_missing_id(db_pool):
      result = await core.reminder_cancel(db_pool, 999999)

      assert result["status"] == "not_found"


  async def test_broadcast_message_is_gated(db_pool):
      session_id = await _new_session(db_pool)

      result = await core.broadcast_message(db_pool, session_id, chat_id=1, text="Heads up everyone")

      assert result["status"] == "pending_confirmation"


  def test_build_core_registry_covers_every_core_tool(db_pool):
      from unittest.mock import AsyncMock

      registry = core.build_core_registry(db_pool, AsyncMock())

      assert set(registry) == {
          "remember_fact", "get_facts", "list_add", "list_show", "list_check_off",
          "list_remove_item", "set_participant", "get_participants",
          "nudge_unconfirmed_participants", "reminder_set", "reminder_cancel",
          "broadcast_message",
      }
  ```

- [ ] **Step 2: Run it, confirm it fails**
  ```bash
  TEST_DATABASE_URL=postgresql://postgres:test@localhost:5432/postgres \
    uv run pytest tests/test_tools_core.py -v
  ```
  Expected: `AttributeError: module 'bot.tools.core' has no attribute 'reminder_set'`.

- [ ] **Step 3: Append to `bot/tools/core.py`**
  ```python
  import functools
  from datetime import datetime, timezone


  async def reminder_set(pool, session_id, chat_id, message, remind_at, target_user_id=None) -> dict:
      when = datetime.fromisoformat(remind_at)
      if when.tzinfo is None:
          when = when.replace(tzinfo=timezone.utc)  # a naive ISO string is treated as UTC
      row = await pool.fetchrow(
          """
          INSERT INTO reminders (session_id, chat_id, target_user_id, message, remind_at)
          VALUES ($1, $2, $3, $4, $5) RETURNING id
          """,
          session_id, chat_id, target_user_id, message, when,
      )
      return {"status": "ok", "reminder_id": row["id"]}


  async def reminder_cancel(pool, reminder_id) -> dict:
      result = await pool.execute(
          "UPDATE reminders SET status = 'cancelled' WHERE id = $1 AND status = 'pending'",
          reminder_id,
      )
      return {"status": "ok"} if result == "UPDATE 1" else {"status": "not_found"}


  async def broadcast_message(pool, session_id, chat_id, text) -> dict:
      return await propose_confirmation(
          pool, chat_id=chat_id, session_id=session_id,
          action_type="broadcast_message", action_params={"text": text},
      )


  def build_core_registry(pool, telegram_bot) -> dict:
      return {
          "remember_fact": functools.partial(remember_fact, pool),
          "get_facts": functools.partial(get_facts, pool),
          "list_add": functools.partial(list_add, pool),
          "list_show": functools.partial(list_show, pool),
          "list_check_off": functools.partial(list_check_off, pool),
          "list_remove_item": functools.partial(list_remove_item, pool),
          "set_participant": functools.partial(set_participant, pool),
          "get_participants": functools.partial(get_participants, pool),
          "nudge_unconfirmed_participants": functools.partial(nudge_unconfirmed_participants, pool, telegram_bot),
          "reminder_set": functools.partial(reminder_set, pool),
          "reminder_cancel": functools.partial(reminder_cancel, pool),
          "broadcast_message": functools.partial(broadcast_message, pool),
      }
  ```

- [ ] **Step 4: Run tests, confirm pass**
  ```bash
  TEST_DATABASE_URL=postgresql://postgres:test@localhost:5432/postgres \
    uv run pytest tests/test_tools_core.py -v
  ```
  Expected: `21 passed`.

- [ ] **Step 5: Commit**
  ```bash
  git add bot/tools/core.py tests/test_tools_core.py
  git commit -m "Add reminder enqueue/cancel, gated broadcast, and build_core_registry"
  ```
