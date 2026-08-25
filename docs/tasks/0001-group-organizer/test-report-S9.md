# Test report: S9 — Message router / end-to-end wiring

**Design:** `docs/tasks/0001-group-organizer/stories/S9-e2e-wiring.md`
**Checked against:** branch `0001-S9-e2e-wiring`
**Date:** 2026-08-25
**Verdict:** PASS — after fixing 4 defects and one production issue found via a flaky test

## Requirement checklist

**R1 — silent capture:** PASS.
`::test_unaddressed_message_records_facts_and_stays_silent` asserts items are
recorded **and** `send_message` is never awaited. Capture runs on the cheap
classifier model, not the tool loop, so ordinary chat does not cost a
primary-model turn per line.

**R2 — participant confirmation:** PASS after Defect 4 (it was unbuilt).
`::test_a_participants_dm_reply_updates_their_status`,
`::test_a_dm_from_someone_with_no_pending_nudge_falls_through`.

**R3 — explicit start/stop only:** PASS. Stop is decided by `classify`, with no
keyword list or regex anywhere in the router.
`::test_addressed_explicit_stop_closes_and_summarizes`,
`::test_addressed_stop_while_snoozed_still_closes` — the case from the user's
own brief: an event silently moved, "not yet" already answered, and the group
now wants the bot gone.

**R5 — addressing is the only trigger:** PASS.
`::test_unaddressed_message_calls_no_model_no_db_no_reply` asserts the
`extract` mock is never awaited — the point of R5 is the absent model call, not
merely the absent reply.

**R6 — tool composition:** PASS, via the tool loop on the addressed path.

**R10 — dedup, decision log, no confident-but-wrong answers:** PASS after
Defect 3. Dedup gates every path including membership updates; every branch
writes a `decision_log` row.

## Defects found during verification

1. **[Medium] Two simultaneous stops posted two summaries** — reproduced with
   `asyncio.gather`. `close_session`'s bool exists for exactly this.
2. **[Medium] A late "да" to an already-closed session posted a full summary**,
   telling the person they closed what the worker had closed hours earlier.
3. **[Medium — R10] A confirmed destructive action reported success blindly**,
   claiming a deletion that may not have happened.
4. **[High — R2 unbuilt] Participants' DM replies went nowhere**, falling into
   the dormant path and attempting to start a session.

## A production defect surfaced by a flaky test

The quiet-hours test failed once with `time zone "US/Hawaii" not recognized`.
Investigating rather than re-running found that **zoneinfo knows 599 zones and
Postgres 487**; the 113 extras are legacy aliases a model would plausibly
produce. Storing one made `claim_sessions_for_closing_question` raise for the
**whole batch** — one chat's bad zone silencing the bot everywhere.
`known_to_postgres` now validates against `pg_timezone_names` on write.
Covered by `::test_a_zone_postgres_cannot_use_is_refused`.

## Verification methods used

No live Telegram and no live model calls. Router tests build real
`python-telegram-bot` objects, so the addressing gate exercises the library's
own UTF-16 entity handling rather than a mock's. Every DB-touching test runs
against a real Postgres. All four defects were reproduced with concrete output
before being fixed. Suite: **218 passed**.
