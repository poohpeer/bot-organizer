# Test report: S7 — Proactive dormant-mode trigger

**Design:** `docs/tasks/0001-group-organizer/stories/S7-proactive-trigger.md`
(requirements in `../EPIC.md`)
**Checked against:** branch `0001-S7-proactive-trigger`, commits `cf2d3e8..HEAD`
**Date:** 2026-08-24
**Verdict:** PASS — after fixing 3 defects found during verification

## Requirement checklist

**R5 — Proactive suggestion from a dormant chat:** PASS (after fixes)

- *"...when a message matches a cheap keyword pre-filter …, then **and only
  then** does the bot run a second, low-cost model check"* — PASS.
  `::test_match_topic_finds_a_keyword_category` covers the filter itself
  (including a non-match). The "and only then" half is the load-bearing part
  and is asserted with a spy rather than assumed:
  `::test_maybe_suggest_skips_when_no_keyword_match` returns
  `no_keyword_match`, and the two new spy tests confirm **zero** `classify`
  calls on the rate-limited and topic-suppressed paths.

- *"...when the chat hasn't received a proactive suggestion in the last 7 days,
  then the bot posts one short, easily-ignorable suggestion"* — PASS after
  Defect 1. `::test_maybe_suggest_posts_when_classifier_confirms` covers the
  happy path (one `send_message`, one row, `response` still NULL).
  `::test_maybe_suggest_enforces_the_weekly_limit_under_concurrency` covers the
  case the original design got wrong.

- *"...when someone replies affirmatively, then a session starts"* — PARTIAL by
  design, not a gap: S7 delivers `get_pending_suggestion` /
  `resolve_suggestion`; the actual session start on "давай" is S9's
  `handle_dormant_message`, and its own brief tests it. Covered here by
  `::test_get_pending_suggestion_returns_unresolved_one`,
  `::test_get_pending_suggestion_none_when_already_resolved`,
  `::test_resolve_suggestion_sets_response`.

- *"...given it's ignored or declined, then the bot says nothing further and
  suppresses suggestions on that same topic in that chat for 30 days"* — PASS.
  `::test_maybe_suggest_topic_suppressed_after_decline`. The mirror case is
  covered too: `::test_an_accepted_topic_is_not_suppressed` — suppression keys
  on `response IS DISTINCT FROM 'accepted'`, so an *accepted* topic stays
  available, which is what R5 asks for and would be easy to break.
  A never-answered suggestion keeps `response = NULL`, which that same
  predicate treats as suppressed — so "ignored" needs no sweeper to work.

- *"...given a chat that already received a proactive suggestion … within the
  last 7 days, then no second-stage check or suggestion happens — the rate
  limit is enforced in code before any model call, not left to prompt
  instructions"* — PASS after Defect 1.
  `::test_maybe_suggest_rate_limited_within_seven_days` plus
  `::test_maybe_suggest_makes_no_model_call_when_rate_limited`.

## Defects found during verification (all fixed on this branch)

Each was **reproduced against a real Postgres before being fixed**, and each
regression test was confirmed to fail against the original implementation
(`git stash` on `bot/proactive.py`) before being kept. The implementation
matched the brief verbatim, so all three were defects in the design.

1. **[High — broke R5] The weekly rate limit was not enforced.** Check and
   insert were separate statements, so two messages arriving together both
   passed. Reproduced with `asyncio.gather`: **2 rows, 2 Telegram messages** in
   one week, against a requirement that names this limit as code-enforced.
   Fixed with `pg_advisory_xact_lock(chat_id)` around a conditional insert.
2. **[High — broke R5] A failed send still silenced the chat for a week.**
   The row was written before `send_message`. On a Telegram refusal the
   exception escaped `maybe_suggest`, the phantom row rate-limited the chat for
   7 days, and `get_pending_suggestion` handed it back — so the next unrelated
   message would have been read as a reply to a suggestion nobody saw. The
   claim is now withdrawn and the failure swallowed (R10: fail toward
   inaction), because S9 runs this on every dormant-chat message.
3. **[Low] `resolve_suggestion` accepted values the schema forbids**, raising
   `CheckViolationError` from inside asyncpg. Now a `ValueError` at the call
   site.

## QA findings beyond the stated criteria

- **[Info] The keyword filter is deliberately narrow and Russian-only.**
  `KEYWORD_TOPICS` is three categories of literal patterns, so plain phrasings
  like "давайте куда-нибудь поедем" do not match and no suggestion is offered.
  That is the correct trade for R5 — a cheap pre-filter that misses is far
  better than one that fires on ordinary chatter — but it does mean recall is
  low by construction, and the epic's "proactive" behavior will trigger rarely.
  Worth revisiting only with real chat logs, not by guessing more patterns.
- **[Info] Category order in `KEYWORD_TOPICS` is significant.** "может, на
  выходных махнём куда-то" matches both `should_go_somewhere` and `lets_go`;
  dict order decides, and the topic chosen becomes the 30-day suppression key.
  Correct as written, but reordering the dict would silently change which
  topic gets suppressed.
- **[Deferred — Low] Nothing ever writes `response = 'ignored'`.** The
  suppression predicate treats NULL as not-accepted, so behavior is right
  without it, but the `'ignored'` state the schema defines stays unused and
  the two cases are indistinguishable in the decision log. Same shape as S4's
  unused `'expired'` confirmation status; both want one sweeper.

## Verification methods used

No live model calls — the Gemini free-tier quota is still exhausted, and
`classify` is monkeypatched throughout (its own live behavior was verified in
S2). Every test runs against a **real Postgres** via the `db_pool` fixture;
Telegram is the only other mocked boundary. The concurrency defect was
reproduced with genuine concurrent coroutines against a real database, not
simulated. Suite: **147 passed**.
