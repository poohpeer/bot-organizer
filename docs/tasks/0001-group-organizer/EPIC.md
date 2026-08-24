# Epic: Group organizer bot

**Goal:** A Telegram bot that lives in a group chat, stays silent by
default, and — only once a human explicitly asks it to — starts an
"organizing session" for one event (a picnic, a trip, a birthday):
listening in plain language, remembering facts and confirmations,
tracking a shared list, nudging non-responders privately, answering
questions grounded in maps/weather/search instead of guessing, and
closing itself down again only with explicit human consent.

**Architecture:** A single `python-telegram-bot` long-polling process
(`bot/`) receives every update, deduplicates it, and routes it through a
per-chat session state machine: **dormant** chats only run a cheap
keyword filter looking for a proactive-suggestion opening; **active**
chats run every message through a Gemini function-calling loop backed by
a small set of generic tools (facts, lists, reminders, web search, maps,
weather) that the model composes freely instead of the app hard-coding
every scenario. All state — sessions, facts, lists, reminders,
confirmations, decision logs — lives in PostgreSQL, written through
plain SQL (no ORM), so a second, independent process (`worker/`) can poll
the same reminder queue and the same session table on a schedule (event
day-after check, idle timeout, due reminders) without ever losing a
delivery to a bot-process restart. The model's only job is turning
natural language into typed tool calls; every fact, list mutation, and
external lookup happens in ordinary, non-fantasizing Python code.

**Tech stack:** Python 3.12, `uv`, `python-telegram-bot>=22` (long
polling), `google-genai` (Gemini, native function calling), PostgreSQL
via `asyncpg` (no ORM), `httpx` for outbound HTTP (Google Places API,
Open-Meteo), `pytest` + `pytest-asyncio`. Deployed as two Kubernetes
workloads (bot + worker) against one Postgres instance.

## Requirements & acceptance criteria

**R1 — Natural-language shared list, no commands**
> As a group member, I want to say "we need tomatoes, cucumbers, and
> meat" in normal chat and have the bot quietly track it, so nobody has
> to learn bot commands to keep a shared list.

Acceptance criteria:
- **Given** an active session, **when** someone writes a message naming one
  or more items to get/bring, **then** the bot adds each item to that
  session's list without announcing it in the chat (silent capture, per R3's
  "no unsolicited chatter" spirit) unless asked.
- **Given** items already on the list, **when** someone asks "what's on the
  list" (in their own words), **then** the bot replies with the current
  list, showing which items are outstanding and which are checked off.
- **Given** an item on the list, **when** someone says they already have it
  ("I already got the cucumbers"), **then** that item is marked checked off,
  matched by name even if phrased differently than when it was added.

**R2 — Participant confirmation tracking + private nudges**
> As the organizer, I want to ask "who's coming" and have the bot check
> who has and hasn't responded, then privately nudge the silent ones, so
> I don't have to chase people manually in the group chat.

Acceptance criteria:
- **Given** an active session with a known set of participants, **when** the
  organizer asks the bot (in chat) to check who's confirmed, **then** the bot
  reports confirmed/declined/no-response per participant it has facts for.
- **Given** participants with no recorded response, **when** the organizer
  asks the bot to check on it, **then** the bot sends each unconfirmed
  participant a private direct message asking if they're coming — never a
  message in the group chat naming them individually.
- **Given** a participant replies to the bot in DM, **when** their reply is
  affirmative or negative, **then** their confirmation status is recorded and
  reflected in the next "who's coming" summary.

**R3 — Explicit-consent session start and stop only**
> As a group member, I don't want the bot guessing when we're "done" —
> a session should only start or stop when a human clearly says so.

Acceptance criteria:
- **Given** a dormant chat, **when** someone explicitly asks the bot to
  start tracking something ("start watching for the picnic"), **then** a
  session begins and the bot confirms in chat that it's now tracking.
- **Given** an active session, **when** someone explicitly tells the bot to
  stop ("that's it, thanks"), **then** the bot posts a short summary and the
  session closes immediately.
- **Given** an active session, **when** the conversation merely trails off,
  moves on, or someone says something that sounds like closure but isn't
  addressed to the bot as an explicit answer to its own closing question
  (R4), **then** the session stays active — the bot never infers closure
  from ambient chat.

**R4 — Bot asks to close itself, with a silence safety net**
> As a group member, I don't want to remember to tell the bot to stop —
> it should ask on its own once the event has clearly passed.

Acceptance criteria:
- **Given** an active session with a known event date, **when** the day after
  that date arrives, **then** the bot posts a closing question in the group
  chat ("how did it go — still need me, or good to close?").
- **Given** a closing question with an explicit "yes, we're done" reply,
  **when** that reply arrives, **then** the bot posts a summary and closes the
  session; **given** an explicit "not yet" reply, **then** the session stays
  active and no further closing question is asked until conditions change.
- **Given** a closing question that gets no reply, **when** ~2 days pass,
  **then** the bot asks once more; **given** no reply again after that,
  **then** the bot auto-closes the session and posts a notice that it did so.
- **Given** a session whose event date was never captured (unparsed or never
  stated), **when** the session has had no activity for 7 days, **then** the
  bot asks the same closing question rather than waiting forever for a date
  that will never arrive.

**R5 — Proactive suggestion from a dormant chat**
> As a group member, I want the bot to notice when we're clearly
> thinking about organizing something, even before anyone remembers it
> exists, so I don't have to be the one who remembers to invoke it.

Acceptance criteria:
- **Given** a dormant chat, **when** a message matches a cheap keyword
  pre-filter for organizing-adjacent phrasing ("we haven't done X in a
  while", "we should go somewhere"), **then** and only then does the bot run
  a second, low-cost model check to confirm it's really an organizing lead
  and not nostalgia or unrelated chat.
- **Given** the second-stage check confirms a lead, **when** the chat hasn't
  received a proactive suggestion in the last 7 days, **then** the bot posts
  one short, easily-ignorable suggestion ("want help organizing that?").
- **Given** a posted suggestion, **when** someone replies affirmatively,
  **then** a session starts (this satisfies R3's consent requirement);
  **given** it's ignored or declined, **then** the bot says nothing further
  and suppresses suggestions on that same topic in that chat for 30 days.
- **Given** a chat that already received a proactive suggestion (accepted,
  declined, or ignored) within the last 7 days, **when** another candidate
  message matches the keyword filter, **then** no second-stage check or
  suggestion happens — the rate limit is enforced in code before any model
  call, not left to prompt instructions.

**R6 — Composable fact memory**
> As a group member, I want to ask things the bot was never specifically
> programmed to answer, like "will it be sunny where we're going", and
> have it figure that out from what it already remembers.

Acceptance criteria:
- **Given** an active session where a destination fact was recorded earlier,
  **when** someone asks a question that requires combining that fact with an
  external lookup (e.g. weather at that place, on that date), **then** the
  bot retrieves the stored fact and calls the relevant lookup tool itself,
  without a purpose-built "check destination weather" function existing in
  the codebase.
- **Given** no matching fact exists for what's asked, **when** the question
  is asked, **then** the bot says it doesn't have that information rather
  than guessing or inventing a plausible-sounding answer.

**R7 — Grounded place-check tied to session context**
> As the organizer, I want to ask "does this place work for us" and get
> an answer about *our* trip, not a generic listing description.

Acceptance criteria:
- **Given** an active session with known constraints (headcount, needs a BBQ
  area, needs parking, a specific date), **when** someone asks the bot to
  check a named place, **then** the bot looks it up via maps/search and
  answers against those specific constraints (e.g. "paid parking, no
  official grilling allowed, rain forecast for Saturday"), not a generic
  rating.
- **Given** a place that returns no usable results from maps or search,
  **when** it's checked, **then** the bot says it couldn't find anything
  rather than fabricating details.

**R8 — "As usual" recency-weighted archive lookup**
> As a group member, I want "same as always?" to actually mean
> something, based on where we've actually been before.

Acceptance criteria:
- **Given** closed sessions in the archive where one place clearly
  dominates when weighted by recency (not just visit count), **when**
  someone asks "where do we usually go" for that activity type, **then** the
  bot names that place along with the evidence (visit count and how
  recent the last visit was).
- **Given** two or more places roughly tied by recency-weighted score,
  **when** asked, **then** the bot lists the top options and asks which one,
  rather than picking one arbitrarily.
- **Given** an activity type where every past place was visited about
  once, with no meaningful pattern, **when** asked, **then** the bot lists
  every place from the archive without trying to pick a favorite.
- **Given** a place visited many times but not recently, versus a place
  visited fewer times but very recently, **when** ranked, **then** recency
  measurably affects the ranking — a stale-but-frequent place does not
  automatically outrank a fresh-but-rarer one.

**R9 — Native location card, captured once**
> As a group member, I want to tap a real map card and get navigation,
> not have someone retype an address.

Acceptance criteria:
- **Given** a place is confirmed for a session for the first time, **when**
  it's resolved via maps, **then** its name and coordinates are stored once
  against that session/place.
- **Given** stored coordinates for a place already confirmed, **when** the
  bot answers a question that names that place, **then** it sends a native
  Telegram venue message (map-preview card with navigate action), not a
  plain-text address, and does not re-query maps for coordinates it already
  has.

**R10 — Reliability guardrails**
> As the group, I want the bot to fail safely rather than confidently
> doing the wrong thing.

Acceptance criteria:
- **Given** a Gemini call or tool-calling parse fails for any reason,
  **when** it happens, **then** the bot replies with a fixed "didn't
  understand, please rephrase" message and performs no side-effecting
  action.
- **Given** a destructive tool call (deleting a list item outright,
  cancelling/closing a session on the model's own initiative, or
  broadcasting a message to every participant), **when** the model proposes
  it, **then** the bot asks for explicit human confirmation in chat before
  executing it — it is never auto-executed from the tool call alone.
- **Given** Telegram redelivers the same update (`update_id` seen before),
  **when** it arrives again, **then** it is dropped before reaching the AI
  layer or any tool, and produces no duplicate list entries, facts, or
  replies.
- **Given** any processed message, **when** it's handled, **then** the raw
  text, the classification/tool-call decisions made, and their parameters
  are written to a decision log, independent of whether the message
  resulted in a visible reply.

**R11 — Reminder delivery survives restarts**
> As the organizer, I want a reminder I set to actually arrive, even if
> the bot process restarts in between.

Acceptance criteria:
- **Given** a reminder is set during an active session, **when** it's
  created, **then** it is persisted to a durable queue table, not held only
  in the bot process's memory.
- **Given** a due reminder in the queue, **when** the independent background
  worker process's poll runs, **then** it sends the reminder and marks it
  delivered, even if the bot process was restarted after the reminder was
  scheduled.
- **Given** multiple reminders due for the same person close together,
  **when** they're delivered, **then** actual sends to that person are
  spaced at least `REMINDER_MIN_INTERVAL_HOURS` apart, enforced by the
  worker at send time, not just at schedule time.

## Global constraints

- Python 3.12+, dependency management via `uv` (`pyproject.toml` +
  `uv.lock`), matching the sibling `general-telegram-bot` project's tooling.
- Telegram integration via `python-telegram-bot>=22`, **long polling**
  (`run_polling()`), not webhooks — avoids the public-HTTPS-tunnel
  requirement that a private group-chat bot doesn't need.
- AI via `google-genai` against Gemini. Models are called only through a
  curated, hand-maintained priority list with sticky fallback on 429/5xx
  (same pattern as the sibling project's `gemini_fallback.py`) — no
  runtime model auto-discovery.
- The two-stage cheap-filter pattern (R5, and the active-mode
  session-relevance filter) always uses the **cheapest** model in the
  fallback chain, never the primary model, for the first-pass check.
- PostgreSQL via `asyncpg`, **no ORM, no migration framework** — schema is
  plain SQL DDL, applied idempotently (`CREATE TABLE IF NOT EXISTS`) by
  both the bot process and the worker process on startup.
- Exactly one **active** session per chat at a time, enforced by a
  partial unique index (`sessions(chat_id) WHERE status = 'active'`), not
  just application logic.
- Every tool the model can call is declared once in `bot/tools/schema.py`;
  the declared parameters must exactly match the Python function signature
  that implements it.
- `REMINDER_MIN_INTERVAL_HOURS` — env var, default `6`. Minimum spacing
  between reminder sends to the same Telegram user id, enforced by the
  worker at send time.
- Proactive suggestions (R5): max 1 per chat per rolling 7 days
  (any topic); a topic that was declined or ignored is suppressed in that
  chat for 30 days from the suggestion timestamp.
- Update dedup (R10): every update's `update_id` is checked against a
  `seen_updates` table before any handler logic with side effects runs.
- Env vars: `BOT_TOKEN`, `GEMINI_API_KEY`, `DATABASE_URL`,
  `GOOGLE_MAPS_API_KEY`. Tests set fake values for `BOT_TOKEN` /
  `GEMINI_API_KEY` / `GOOGLE_MAPS_API_KEY` in `tests/conftest.py`
  (mirroring the sibling project's pattern); DB-touching tests require a
  real, reachable Postgres via `TEST_DATABASE_URL` — start one locally
  with `docker run --rm -d -p 5432:5432 -e POSTGRES_PASSWORD=test postgres:16`
  and set `TEST_DATABASE_URL=postgresql://postgres:test@localhost:5432/postgres`.
- Weather via Open-Meteo (free, keyless, plain HTTP). Maps/places via the
  Google Places API (Text Search + Place Details), keyed by
  `GOOGLE_MAPS_API_KEY`. Both called through one shared `httpx.AsyncClient`.
- Replies are plain text (no Markdown/HTML rendering needed for v1) with
  one exception: confirmed places are delivered as native Telegram venue
  messages (`Bot.send_venue`), per R9.

## Story index

| ID | Title | File | Satisfies | Depends on | Parallel group |
|----|-------|------|-----------|-----------|----------------|
| S1 | Data model & persistence | stories/S1-data-model.md | R1, R2, R4, R5, R8, R9, R10, R11 | — | A |
| S2 | AI layer: Gemini, fallback, tool-calling harness, cheap classifier | stories/S2-ai-layer.md | R5, R6, R10 | — | A |
| S3 | Session lifecycle: state machine, closing-question policy, dedup, decision log | stories/S3-session-lifecycle.md | R3, R4, R10 | S1 | B |
| S4 | Core tools: facts, lists, participants, reminders, confirmation gate | stories/S4-core-tools.md | R1, R2, R6, R10, R11 | S1, S2 | B |
| S5 | External grounding tools: web search, maps, weather | stories/S5-external-tools.md | R6, R7, R9, R10 | S2 | B |
| S6 | Composed flows: place-check, "as usual" archive ranking, venue messages | stories/S6-composed-flows.md | R7, R8, R9 | S1, S5 | C |
| S7 | Proactive dormant-mode trigger | stories/S7-proactive-trigger.md | R5 | S1, S2, S3 | C |
| S8 | Background worker: reminder delivery + closing-question firing | stories/S8-background-worker.md | R4, R11 | S1, S3 | C |
| S9 | Message router / end-to-end wiring | stories/S9-e2e-wiring.md | R1, R2, R3, R5, R6, R10 | S2, S3, S4, S5, S6, S7 | — |
| S10 | Deployment: Dockerfile, Kubernetes manifests | stories/S10-deployment.md | (infra; no new R) | S8, S9 | — |

Every requirement R1–R11 appears in at least one story's `Satisfies` cell.

## Dependency graph

```
S1 ──┬─▶ S3 ──┬────────────────────┐
     │        ├─▶ S7 (needs S2)    │
     │        └─▶ S8               ├─▶ S9 ──▶ S10
     ├─▶ S4 (needs S2)             │
     └─▶ S6 (needs S5) ────────────┘
S2 ──┴─▶ S5 ──▶ S6
```

Concretely (derived from each story's actual `Consumes` — an import, not
just a shared table, counts as a dependency):
- S1, S2 — no dependencies.
- S3 depends on S1.
- S4 depends on S1, S2.
- S5 depends on S2.
- S6 depends on S1, S5 (`resolve_and_save_place` calls `bot.tools.external.maps_lookup`;
  it does **not** import anything from S4 — the model composes S4's and
  S6's tools together only at the S9 wiring layer, not in code).
- S7 depends on S1, S2, S3 (`bot.proactive` calls `bot.ai.classify.classify`
  (S2) and `bot.decision_log.log_decision` (S3)).
- S8 depends on S1, S3 (`worker.closing` calls `bot.session` functions;
  `worker.reminders` only touches the `reminders` table directly — no S4
  import).
- S9 depends on S2, S3, S4, S5, S6, S7.
- S10 depends on S8, S9.

## Parallel groups

- **Group A (no deps):** S1, S2 — disjoint files (`db/` vs `bot/ai/`) —
  may run concurrently.
- **Group B (after Group A):** S3, S4, S5 — three disjoint modules
  (`bot/session.py`+`bot/dedup.py`+`bot/decision_log.py`, `bot/tools/core.py`,
  `bot/tools/external.py`) with no dependencies on each other — may all
  run concurrently once Group A is done.
- **Group C (after Group B):** S6, S7, S8 — disjoint files
  (`bot/tools/composed.py`, `bot/proactive.py`, `worker/`) — S6 needs S5,
  S7 needs S2+S3, S8 needs S3; none of the three needs another from this
  group — may run concurrently once Group B is done.
- S9 and S10 run sequentially at the end — S9 touches `bot/main.py`/
  `bot/router.py` and necessarily depends on nearly everything; S10
  packages both entrypoints once they exist.

## Execution notes

A full epic is implemented on a single feature branch with a commit per
story; the implementer runs units sequentially in dependency order
(parallel groups are informational — record true independence, but
execution is still sequential) and leaves the branch for the user to
review and merge — it does not merge, push, or open PRs.
