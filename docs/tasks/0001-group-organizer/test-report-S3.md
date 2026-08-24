# Test report: S3 — Session lifecycle, closing-question policy, dedup, decision log

**Design:** `docs/tasks/0001-group-organizer/stories/S3-session-lifecycle.md`
(requirements in `../EPIC.md`)
**Checked against:** branch `0001-S3-session-lifecycle`, commits `c72cc37..d996cf0`
**Date:** 2026-08-24
**Verdict:** PASS — after fixing 3 defects found during verification (details below)

## Scope note

S3 declares `Satisfies: R3, R4, R10`. R3/R4 are stated end-to-end ("the bot
posts a closing question", "the bot confirms in chat"), and the chat-facing
halves belong to S8 (worker posts the question) and S9 (router interprets
"start"/"that's it, thanks"). S3 owns the state machine underneath: when a
session may start/stop, when a closing question becomes due, when to re-ask,
when to give up, and the dedup/decision-log primitives. That layer is what
this report exercises against a real Postgres.

## Requirement checklist

**R3 — Explicit-consent start and stop only:** PASS (S3's share)
- *"...when someone explicitly asks the bot to start tracking, then a session
  begins"* — `test_session.py::test_start_session_creates_active_session`,
  plus `::test_start_session_rejects_second_active_session` and
  `::test_get_active_session_returns_none_when_dormant` for the surrounding
  states. The one-active-session-per-chat invariant is enforced by a partial
  unique index, not just the pre-check, and the TOCTOU race is covered by
  catching `UniqueViolationError`.
- *"...when someone explicitly tells the bot to stop, then the session closes
  immediately"* — `::test_close_session_marks_closed_with_reason`.
- *"...when the conversation merely trails off or moves on, then the session
  stays active — the bot never infers closure from ambient chat"* — verified
  end-to-end in
  `test_session_lifecycle.py::test_r3_ambient_activity_never_closes_a_session`:
  five `touch_activity` calls leave the session active and produce no closing
  question. Interpreting *which* utterances count as explicit is S9's job.

**R4 — Bot asks to close itself, with a silence safety net:** PASS (after fixes)
- *"...when the day after the event date arrives, then the bot posts a closing
  question"* — `test_session_lifecycle.py::test_r4_unanswered_closing_question_ends_in_auto_close`,
  which walks the full arc against a real database: ask on day-after → quiet
  on the same tick → one re-ask after 2 days of silence → auto-close after 2
  more, ending `status='closed'`, `closed_reason='auto_close_silence'`.
- *"...given an explicit 'not yet' reply, then the session stays active and no
  further closing question is asked until conditions change"* — this is the
  criterion the original implementation **failed**; see Defect 1. Now covered
  by `::test_r4_not_yet_keeps_the_session_alive_and_quiet`, which asserts
  silence across the whole snooze window, and by
  `test_session.py::test_snooze_expires_and_the_question_becomes_due_again`
  for the other side (it does eventually come back).
- *"...given no reply, then ask once more; given no reply again, auto-close and
  post a notice"* — same lifecycle test; the notice text itself is S8's.
- *"Given a session whose event date was never captured, when it has had no
  activity for 7 days, then the bot asks the same closing question"* —
  `test_session.py::test_needs_closing_question_when_idle_a_week_and_no_date`,
  with `::test_not_due_yet_when_recently_active_and_no_date` as the negative.

**R10 (S3's share) — dedup and decision log:** PASS
- *"...when Telegram redelivers the same update_id, then it is dropped before
  reaching the AI layer or any tool"* — `test_dedup.py` (2 tests): first
  sighting `False`, second `True`. The insert-and-check is a single
  `INSERT ... ON CONFLICT DO NOTHING`, so it is atomic rather than
  check-then-insert. Wiring it ahead of the handler is S9's.
- *"...the raw text, the decisions made, and their parameters are written to a
  decision log, independent of whether the message resulted in a visible
  reply"* — `test_decision_log.py` (2 tests), including a null `user_id`.
  Nested dicts round-trip as dicts via the S1 jsonb codec.

## Defects found during verification (all fixed on this branch)

Found by `code-review:code-review`, then each **reproduced against a real
database before being fixed** — they were not hypothetical:

1. **[High — R4 violation] Closing-question re-ask loop.** A "not yet" reply
   only cleared `closing_question_asked_at`, so a session whose `event_date`
   was in the past immediately re-matched the first due-branch. Reproduced:
   after recording "not yet", the session was due again on the very next
   check (`due=1`), meaning the bot would ask "still need me?" on every worker
   tick forever — the precise nagging behavior R4 exists to prevent. The idle
   branch had the same flaw. Fixed with a `closing_question_snoozed_until`
   column that all due-branches and the auto-close respect.
2. **[Medium] Non-idempotent close.** `close_session` had no
   `status = 'active'` guard. Reproduced: auto-closing then explicitly closing
   the same session left `closed_reason='explicit_stop'`, overwriting the real
   reason — and both callers would have posted a closing summary. Now guarded
   and returns whether it applied; `record_closing_reply` likewise, so a late
   "not yet" can't silently mutate a closed row.
3. **[Medium] Double-post race.** Select-then-mark let two pollers claim the
   same session. Replaced with `claim_sessions_for_*` helpers that mark/close
   atomically (`UPDATE ... RETURNING ... FOR UPDATE SKIP LOCKED`); verified by
   two consecutive claims where the second returns empty. S8's story file was
   updated to consume them.

## QA findings beyond the stated criteria

- **[Deferred — Low] Timezone.** `event_date < current_date` is evaluated in
  the database's timezone (UTC). For a group at UTC+3 the day-after question
  can fire a few hours into the wrong local day. A correct fix needs a
  per-chat timezone the schema doesn't carry; acceptable for v1
  single-timezone groups, recorded in the story's implementation notes.
- **[Deferred — Low, deliberate] Dedup marks before handling.**
  `is_duplicate` records the `update_id` before the handler runs, so a crash
  mid-handler drops that update instead of reprocessing it on redelivery.
  Kept as-is: R10 explicitly asks that degradation be toward inaction, and the
  alternative risks duplicate list items and facts — the exact "бот тупит"
  symptom the idea doc calls out.
- **[Info] Snooze length is a fixed 7 days** (`session.SNOOZE_DAYS`), not
  derived from the event. A group that says "not yet, we're going again next
  month" gets re-asked at 7 days rather than after the new date. Acceptable —
  the re-ask is a single easily-ignored question — but a natural refinement
  once S9 can extract a new date from that reply.

## Verification methods used

Everything in the checklist was **run against a real Postgres** (the
`db_pool` fixture, throwaway schema per test), not inspected. The three
defects were reproduced with failing assertions before the fix and are now
locked in by regression tests. Suite: `uv run pytest tests/ -q` → **50
passed** (39 after implementation, +8 regression tests for the defects, +3
lifecycle integration tests). No mocks stand in for the database anywhere in
S3's coverage.
