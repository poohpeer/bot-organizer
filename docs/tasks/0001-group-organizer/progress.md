# Progress: Group organizer bot (0001)

**Status:** in_progress
**Merge policy:** auto-merge

**Test Postgres:** local Docker container `bot-organizer-test-pg`,
`TEST_DATABASE_URL=postgresql://postgres:test@localhost:5432/postgres`.

## Units
| Unit | Branch | Status | Commits | Verification | PR | Notes |
|------|--------|--------|---------|--------------|----|-------|
| S1 | 0001-S1-data-model | complete | squashed as 10248da | review clean; tester PASS | #2 (merged) | |
| S2 | 0001-S2-ai-layer | complete | dabe8d7..5b1da69 | review: 7 findings fixed; tester PASS | — | classifier + tool loop verified against live API |
| S3 | 0001-S3-session-lifecycle | complete | c72cc37..d996cf0 | review: 3 defects fixed; tester PASS | — | R4 re-ask loop caught + fixed |
| S4 | 0001-S4-core-tools | complete | 806c251..HEAD | review: 8 defects fixed; tester PASS | — | R1+R10 verified live |
| S5 | 0001-S5-external-tools | complete | e5938e6..HEAD | review: 6 defects fixed; tester PASS | — | maps not live-verified (no API key) |
| S6 | 0001-S6-composed-flows | complete | 7a0813b..HEAD | review: 6 defects fixed; tester PASS | — | maps verified LIVE (key supplied) |
| S7 | 0001-S7-addressing-gate | complete | see PR | tester PASS | — | REPLACED the proactive trigger (PR #8) after a product decision |
| S8 | 0001-S8-background-worker | complete | 02a44e6..HEAD | review: 5 defects fixed; tester PASS | — | R4 auto-close-without-asking caught |
| S9 | — | blocked | — | — | — | brief rewritten for the addressing gate |
| S10 | — | blocked | — | — | — | waits on S8, S9 |

## Findings to address
- Deferred (S3/S4): no per-chat timezone. Affects event_date and reminder
  delivery times. The main outstanding user-visible debt.
- Deferred (S4): nothing writes the 'expired' status on stale
  pending_confirmations.
- Limitation (S6): Places returns no review snippets on this key (needs the
  Enterprise + Atmosphere SKU), so R7 grounding leans on web_search.

## Log
- 2026-08-24: Design merged to main. Test Postgres started. Beginning S1.
- 2026-08-24: S1 implemented (2 tasks), code-review clean (2 findings
  fixed: db_pool fixture leak on early failure, progress ledger staleness),
  tester PASS (see test-report-S1.md). Merged as PR #2.
- 2026-08-24: S2 implemented (2 tasks). Code review raised 9 findings; 7
  genuine and fixed with regression tests, 1 (Gemma lacks system
  instructions/structured output/function calling) disproven against the
  live API, 1 (import-time KeyError on GEMINI_API_KEY) intentional per the
  epic's crash-on-missing convention. Tester PASS — R6 tool composition
  and R5 classification verified against the live Gemini API, not mocks.
  A real 429 during verification exercised the new mid-conversation
  fallback path and recovered. Suite: 23 passed.
- 2026-08-24: S3 implemented (3 tasks, clean first pass). Code review found
  8 issues; 3 fixed as real defects (all reproduced against a real DB first):
  an R4-violating closing-question re-ask loop, a non-idempotent close that
  overwrote closed_reason on a race, and a select-then-mark double-post race.
  Added closing_question_snoozed_until to the schema and atomic claim_*
  helpers (S8's story updated to match). 2 findings deferred with rationale
  (timezone, dedup-before-handler). Tester PASS. Suite: 50 passed.
- 2026-08-24: S4 implemented (5 tasks, clean first pass against the brief).
  Code review found 11 issues; 8 fixed as real defects (each reproduced
  against a real DB first), incl. duplicate participant rows breaking R2, the
  nudge aborting on the first Forbidden, a gated destructive action executing
  twice, and model-supplied chat_id allowing cross-chat writes (chat_id
  removed from the tool declarations). R1 and R10 verified end-to-end against
  the live model. Deferred: per-chat timezone (shared with S3 — the main
  outstanding debt), expired-confirmation sweeper. Suite: 82 passed.
- 2026-08-24: S5 implemented (2 tasks) with good hardening from the subagent.
  Code review found 7 issues, 6 fixed — incl. web_search having no model
  fallback (observed failing live when the Gemini quota ran out) and
  maps_lookup returning name=None which would break S6's NOT NULL insert.
  weather_lookup verified live against Open-Meteo; maps_lookup is mock-only
  (no GOOGLE_MAPS_API_KEY available) — flagged as the weakest evidence so far.
  NOTE: Gemini free-tier quota exhausted during verification; further live
  model checks may be rate-limited until it resets. Suite: 109 passed.
- 2026-08-24: S6 implemented (3 tasks, clean first pass against the brief).
  Code review + verification found 6 design defects, each reproduced first:
  the place cache matched only the canonical maps name (R9 — re-queried maps
  and duplicated rows on the group's own phrasing for a place), model-supplied
  chat_id on BOTH send_location and archive_lookup (the same cross-chat hole
  S4 closed, write side and read side), a TypeError on a closed session with
  no dates, a future event_date outscoring real history, and 'tied' being
  reportable with a single option. Added places.query + a per-session unique
  index, and a declaration/signature cross-check test (verified it catches
  drift). The user supplied a GOOGLE_MAPS_API_KEY, so R7/R9 were verified
  against the LIVE Places API for the first time — closing S5's weakest
  evidence gap. Found that review_snippets is always empty on this key.
  Suite: 131 passed.
- 2026-08-24: S7 implemented (2 tasks, clean first pass against the brief).
  Verification against a real DB found 3 design defects, each reproduced
  first and each regression test confirmed to fail against the original code:
  the R5 weekly rate limit was not actually enforced (two concurrent messages
  both suggested — fixed with pg_advisory_xact_lock around a conditional
  insert), a failed Telegram send left a phantom suggestion that silenced the
  chat for 7 days and would be misread as pending (now withdrawn, and no
  longer raises — S9 runs this on every dormant message), and
  resolve_suggestion let a schema-forbidden value through to asyncpg.
  Confirmed correct without change: zero model calls on the rate-limited and
  suppressed paths, which is R5's explicit wording. Suite: 147 passed.
- 2026-08-24: PRODUCT PIVOT. The bot must never decide on its own that help
  is wanted — it acts only when @mentioned or replied to. R5 was rewritten
  from "proactive suggestion" to "explicit addressing is the only trigger",
  and the S7 merged an hour earlier (PR #8) was removed: bot/proactive.py,
  its 16 tests, and the proactive_suggestions table are gone. Replaced by
  bot/addressing.py — a model-free gate (17 tests). Two Telegram facts
  checked against the docs, both load-bearing: privacy mode is ON by default
  and does NOT deliver plain @mentions (so it must be disabled in BotFather
  or the bot is unreachable, since the epic forbids commands), and an admin
  bot receives everything regardless (so the gate cannot rely on the
  platform). Two defects caught by tests before commit: parse_entity raises
  on caption-only messages, and UTF-16 entity offsets break naive slicing
  (one emoji before a mention was enough). R3/R4 amended per the user:
  stopping is model-understood with no keyword list anywhere, the event date
  may live only in the chat title, and an explicit human stop always
  overrides R4's snooze. S9's brief rewritten — luckily it was not yet
  built. Suite: 148 passed.
- 2026-08-24: S8 implemented (3 tasks, clean first pass against the brief).
  Verification found 5 design defects, each reproduced first: one unreachable
  recipient stranded the whole reminder batch, a doomed reminder retried
  every 60s forever (added attempts + a 'failed' status), two workers
  double-delivered the same reminder, group reminders were rate-limited
  against the epic's own per-user-id constraint, and — worst — a closing
  question that failed to send was still recorded as asked, so all three test
  sessions would have auto-closed two days later without the group ever being
  asked (R4 violation, caused by my own S3 reasoning; that docstring is now
  corrected). Also isolated the three poll steps. Checked and cleared: a bare
  telegram.Bot outside `async with` is fine on PTB 22.8. Suite: 165 passed.
  NOTE: the per-chat timezone debt is now VISIBLE — S8 is what delivers at
  the wrong local time. Fix before real use.
