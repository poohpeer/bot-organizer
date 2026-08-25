# Progress: Group organizer bot (0001)

**Status:** complete
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
| S9 | 0001-S9-e2e-wiring | complete | 0ab6507..HEAD | review: 4 defects + 1 prod issue fixed; tester PASS | — | R2 was unbuilt; zoneinfo/Postgres zone mismatch found |
| S10 | 0001-S10-deployment | complete | see PR | stack built and run; tester PASS | — | bot reached Telegram from the container |
| S11 | 0001-S11-per-chat-timezone | complete | see PR | tester PASS | — | added mid-epic; closes the timezone debt from S3/S4/S8 |

## Findings to address
- RESOLVED in S11: per-chat timezone. Reminders and the closing question now
  use the chat's own zone.
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
- 2026-08-24: S11 (added mid-epic at the user's request) closes the timezone
  debt carried since S3. Telegram exposes no timezone, so it is resolved in
  layers: DEFAULT_TIMEZONE env -> chats.timezone -> a free Open-Meteo lookup
  at a resolved place's coordinates -> an explicit set_timezone tool. Layer 3
  is called from CODE on place resolution, not from weather_lookup: that tool
  only runs when the model thinks weather matters, and never when a reminder
  is set before a place is chosen. Per the user, reminder_set now reports
  timezone_assumed so the bot names its assumption and asks for a rough
  location, and set_timezone re-anchors already-scheduled reminders from the
  stored wall-clock time (exact across DST). Closing questions now follow the
  chat's local day, not the server's. The local-day test was confirmed to fail
  against the old current_date logic. Suite: 181 passed.
- 2026-08-25: Added quiet hours to S11 before moving on. Fixing the local day
  had made the closing question due at local midnight; QUIET_UNTIL_HOUR=9 /
  QUIET_FROM_HOUR=21 now gate it and the auto-close notice in the chat's own
  zone, delaying rather than skipping. Tested by placing a chat in whichever
  IANA zone is currently at the hour under test — no faked clocks. Both
  "stay silent" tests confirmed to fail with the window removed. Suite: 184.
- 2026-08-25: S9 implemented (3 tasks). Brief was handed over as a SPEC, not
  code — the pre-pivot version still flattened Message to text, which the
  addressing gate cannot use. Review found 4 defects, each reproduced: two
  simultaneous stops posted two summaries, a late 'да' to an already-closed
  session posted a summary as if that person closed it, a confirmed
  destructive action reported success blindly (R10), and — flagged honestly
  by the subagent — R2 was entirely unbuilt: participants' DM replies fell
  into the dormant path and tried to start a session. Added
  handle_private_message. Separately, a flaky quiet-hours test turned out to
  be a PRODUCTION defect: zoneinfo knows 599 zones, Postgres 487, and storing
  one of the 113 legacy aliases made the closing-question query raise for the
  whole batch — one chat's bad zone silencing the bot everywhere. Zones are
  now validated against pg_timezone_names on write. Suite: 218 passed.
- 2026-08-25: S10 — Dockerfile + docker-compose (postgres/bot/worker), written
  and verified in the parent session by actually building and running the
  stack. The bot resolved its identity through Telegram FROM INSIDE the
  container (id=8998516801 username=pooh_organizer_bot), which proves image,
  env plumbing, network and token together. Verified rather than assumed: 10
  tables created by the app's own DDL, no .env or secret values in the image,
  a row surviving both --force-recreate and a full down/up, and the
  unless-stopped policy restarting a self-exiting container 5 times in 18s.
  Two brief defects fixed: .env.example lacked the three variables S11 added,
  and the healthcheck tested the server rather than the database. A method
  correction is recorded — the first restart test used `docker kill`, which
  correctly does NOT restart, since unless-stopped must not override a manual
  stop.
  EPIC COMPLETE: 10 stories + S11, 218 tests. Remaining: a live group test,
  which needs a human to add @pooh_organizer_bot to a chat and talk to it.
