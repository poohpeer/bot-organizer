# Test report: S1 — Data model & persistence

**Design:** `docs/tasks/0001-group-organizer/stories/S1-data-model.md`
**Checked against:** branch `0001-S1-data-model`, commits `d6416ad..c601cb0`
**Date:** 2026-08-24
**Verdict:** PASS (foundational infra story — see scope note)

## Scope note

S1 is a foundational infra story (`bot/tools/schema.py` doesn't exist
yet, no message routing exists yet). None of the epic's user-facing
Given/When/Then criteria (R1–R11, defined in `EPIC.md`) are actually
exercisable end-to-end yet — they require S9 (message router), which
depends on S2–S8. S1's own "definition of done" is its stated
**Produces** contract: the Postgres schema (11 tables, exact columns,
constraints) and the `create_pool`/`init_db`/`db_pool` primitives every
later story builds on. This report verifies that contract directly,
since that's what a fresh subagent picking up S2–S9 will actually rely
on.

## Contract checklist (from S1's Interfaces: Produces)

**`db.pool.create_pool(dsn, *, init=None) -> asyncpg.Pool`:** PASS
- Ran directly (not just via the test suite): built a pool against the
  real test Postgres, confirmed the caller's `init` callback (schema
  pinning via `SET search_path`) survives multiple `pool.acquire()`
  cycles, not just the first connection — `verify_s1.py` result:
  `PASS - search_path pinned across acquires`.
- This directly re-verifies the fix from the code-review pass: the
  brief's sample code used asyncpg's `init=` hook (fires once per
  physical connection) for the caller's callback; the implementation
  correctly moved it to `setup=` (fires on every acquire) instead,
  because asyncpg resets session state on release-back-to-pool. Without
  this fix, any pool call after the first acquired connection would
  silently escape the pinned schema and hit `UndefinedTableError`
  against the wrong (public) schema.

**`db.pool.init_db(pool) -> None` — idempotent schema application:** PASS
- `tests/test_db_pool.py::test_init_db_creates_all_tables` — ran for
  real (`uv run pytest`, not inspection): asserts the exact set of 11
  tables (`chats`, `sessions`, `facts`, `participants`, `list_items`,
  `reminders`, `places`, `proactive_suggestions`, `seen_updates`,
  `decision_log`, `pending_confirmations`) exists in the pool's schema.
  `2 passed`.
- `CREATE TABLE IF NOT EXISTS` / `CREATE INDEX IF NOT EXISTS` make this
  safe to call from both `bot/main.py` and `worker/main.py` on every
  startup (per S9/S8's design) — confirmed idempotent by calling
  `init_db` twice against the same pool in `verify_s1.py` with no error.

**One active session per chat (partial unique index):** PASS
- `tests/test_db_pool.py::test_only_one_active_session_per_chat` — ran
  for real: inserting a second `status='active'` row for the same
  `chat_id` raises a unique-violation. `2 passed`.

**JSONB round-trip as plain Python dicts:** PASS
- Verified manually via `verify_s1.py` (nested dict round-trip through
  `decision_log.decision`), then added a committed regression test —
  `tests/test_db_pool.py::test_jsonb_columns_round_trip_as_python_dicts`
  — so this is no longer only ad-hoc-script-verified. `3 passed` (full
  suite, after the addition).

**CHECK/FK constraints enforce the schema's invariants:** PASS
- Verified for real (`verify_s1.py`): `sessions.status` rejects a value
  outside `('active','closed')` (`CheckViolationError`); `facts` rejects
  an insert referencing a nonexistent `session_id`
  (`ForeignKeyViolationError`).

**`db_pool` pytest fixture — schema isolation + cleanup:** PASS
- Ran for real: a throwaway test captured its fixture-assigned schema
  name (`test_b79538012cd7`), and after that test's teardown a separate
  connection confirmed `information_schema.schemata` no longer contains
  it — the fixture's `DROP SCHEMA ... CASCADE` teardown actually runs.
- Also re-verified the code-review fix (try/finally around create/drop)
  didn't change this observable behavior — cleanup still happens on the
  normal (non-error) path.

## QA findings beyond the stated criteria

- **[Info]** `db_pool`'s teardown-on-failure path (the code-review fix)
  wasn't separately exercised by a test that forces `init_db` to fail —
  I confirmed the *normal* teardown path still drops the schema, but
  didn't force an exception before `yield` to confirm the `finally`
  branch specifically triggers in that case. The `try/finally` structure
  is straightforward enough that this is very low risk, but noting it
  for completeness since it was the code-review finding being fixed.

## Verification methods used

Ran for real against the actual test Postgres
(`TEST_DATABASE_URL=postgresql://postgres:test@localhost:5432/postgres`)
throughout — the committed test suite (`uv run pytest tests/ -v`, 2
passed) plus a standalone verification script (`verify_s1.py`) and two
throwaway fixture-behavior probes, all executed directly, not inferred
from code reading. No criterion in this report was verified by
inspection only.
