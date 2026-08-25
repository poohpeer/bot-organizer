# Story S9: Message router / end-to-end wiring

**Part of:** Group organizer bot (`../EPIC.md`)
**Goal:** The single place that decides, for every incoming Telegram
message, whether the chat is dormant or active and which of the special
cases (session start, session stop, closing-question reply, pending
destructive-action confirmation) applies before falling through to the
general tool-calling loop — then the `python-telegram-bot` `Application`
that drives it.
**Satisfies:** R1, R2, R3, R5, R6, R10
**Depends on:** S2, S3, S4, S5, S6, S7
**Parallel-safe with:** none (final integration)
**Requirements & global constraints:** see `../EPIC.md`

**Two rules bind every task below:**

1. **The bot acts only when addressed** (R5). `bot.addressing` decides that,
   with no model involved. In a dormant chat an unaddressed message is
   dropped before any model call. In an *active* session an unaddressed
   message is still read and its facts recorded (R1's silent capture), but
   the bot does not reply.
2. **Stopping is understood, never matched** (R3). There is no keyword list
   or regular expression for "we're done" anywhere in the router. An
   addressed message goes to the model, which decides among: answer, start,
   stop. Any phrasing that means "you're not needed" must close the session,
   including when the event was silently moved and R4's snooze is still
   running — an explicit human stop always wins.

---

### Task 1: Dormant-chat routing

**Satisfies:** R3, R5

**Files:**
- Create: `bot/router.py`
- Create: `tests/test_router.py`

**Interfaces:**
- Consumes: `bot.addressing.addressed_to_bot`/`bot_was_added` (S7),
  `bot.session.start_session`/`SessionAlreadyActiveError` (S3),
  `bot.ai.classify.extract` (S2), `bot.decision_log.log_decision` (S3).
- Produces:
  - `async bot.router.handle_dormant_message(pool, telegram_bot, message, bot_id, bot_username) -> None`
  - `async bot.router.handle_bot_added(pool, telegram_bot, chat_member_updated, bot_id, bot_username) -> None`

**Routing rule (R5) — no model runs before the gate:**

```
addressed_to_bot(message, bot_id, bot_username)?
├── no  ──▶ return immediately. No model call, no DB write, no reply.
└── yes ──▶ extract activity_type + event_date from the message, using
            chat.title as additional context (R4: the date may live only
            in the chat title, e.g. "Пикник 15 сентября").
            ├── extraction confident ──▶ start_session, confirm in chat
            └── not confident        ──▶ ask what to track; no session yet
```

`handle_bot_added` posts one short message explaining how to call the bot
(mention or reply) and **starts no session** — being added is not consent.
It fires only on an actual join: `bot_was_added` returns False for a
promotion, so a permission change never re-greets the chat.

**Tests must cover, at minimum:**
- An unaddressed message in a dormant chat: no reply, and the `extract`
  mock is **not awaited** — R5's "no model call" is the point, so assert it
  directly rather than only asserting silence.
- An addressed message naming an activity: a session starts and the bot
  confirms.
- An addressed message whose event date appears only in `chat.title`: the
  session's `event_date` is taken from the title.
- An addressed message too vague to extract an activity: the bot asks what
  to track and starts no session.
- `SessionAlreadyActiveError`: the bot says it is already tracking, and does
  not create a second session.
- Bot added to a chat: exactly one greeting, no session row.
- Bot promoted: nothing sent.


### Task 2: Active-session routing

**Satisfies:** R1, R2, R3, R6, R10

**Files:**
- Modify: `bot/router.py`
- Modify: `tests/test_router.py`

**Interfaces:**
- Consumes: `bot.addressing.addressed_to_bot` (S7),
  `bot.session.close_session`/`record_closing_reply`/`touch_activity` (S3),
  `bot.tools.core.get_pending_confirmation`/`resolve_confirmation`/
  `execute_confirmed_action` (S4), `build_core_registry` (S4),
  `build_external_registry` (S5), `build_composed_registry` (S6),
  `bot.ai.tool_loop.run_tool_loop`, `bot.ai.client.fallback`,
  `bot.ai.classify.extract` (S2), `bot.decision_log.log_decision` (S3).
- Produces:
  `async bot.router.handle_active_message(pool, telegram_bot, active_session, message, bot_id, bot_username) -> None`

**Listening is not speaking (R5 + R1).** An active session reads every
message and answers only addressed ones:

```
addressed_to_bot?
├── no  ──▶ SILENT CAPTURE. Extract any facts/list items worth keeping and
│           record them, touch_activity, send NOTHING. Never send_message
│           on this path — R1's "adds each item without announcing it".
└── yes ──▶ 1. an outstanding closing question?  -> record_closing_reply
            2. a pending destructive confirmation? -> resolve + execute
            3. otherwise -> classify intent, then the full tool loop
```

**Stopping is understood, never matched (R3).** There is no keyword list or
regular expression for "we're done" anywhere in the router. The addressed
path asks the model to classify intent among `stop` / `continue`, and any
phrasing meaning "you're not needed" must close the session — including
while R4's `closing_question_snoozed_until` is still in the future. The
snooze suppresses the bot's *own* asking, never a human's instruction.

**Tests must cover, at minimum:**
- Unaddressed message in an active session: facts are recorded,
  `touch_activity` called, and `telegram_bot.send_message` is **never**
  awaited. Assert the silence directly — this is R1's whole behaviour.
- Unaddressed message: the tool loop is **not** run (no reply means no
  expensive turn).
- Addressed "всё, спасибо, свободен": session closed with
  `explicit_stop`, a summary posted.
- Addressed stop **while snoozed**: still closes. This is the case from
  the user's own brief — an event silently moved, "not yet" already
  answered, and the group now wants the bot gone.
- Several different stop phrasings all close the session, driven by the
  classifier rather than a pattern list.
- Addressed message answering an outstanding closing question ("да, всё"
  / "нет, ещё нужен") routes to `record_closing_reply`, not to the tool
  loop.
- Addressed "да" while a destructive confirmation is pending: resolves and
  executes it, and does **not** fall through to the tool loop.
- Addressed ordinary request: the tool loop runs and its reply is posted.
- An empty reply from the tool loop posts nothing.
- Every branch writes a `decision_log` row (R10).


---

### Task 3: `bot/main.py` — Telegram application wiring

**Satisfies:** R5, R10

**Files:**
- Create: `bot/main.py`
- Create: `tests/test_main.py`

**Interfaces:**
- Consumes: `bot.router` (Tasks 1–2), `bot.dedup.is_duplicate` (S3),
  `bot.session.get_active_session` (S3), `bot.addressing.bot_was_added` (S7),
  `db.pool.create_pool`/`init_db` (S1).
- Produces:
  - `async bot.main.route_update(pool, telegram_bot, message, bot_id, bot_username, *, update_id) -> None`
  - `async bot.main.route_membership(pool, telegram_bot, chat_member_updated, bot_id, bot_username) -> None`
  - `bot.main.main()` — the bot process entrypoint.

**The whole Message object is passed through**, not extracted text: the
addressing gate needs `entities`, `caption_entities` and
`reply_to_message`, none of which survive being flattened to a string.

**`bot_username` comes from `await telegram_bot.get_me()` at startup.**
Never a constant or an env var: if the bot is renamed, a stale username
makes every mention stop matching and the bot goes permanently silent with
no error in the logs — the hardest possible failure to diagnose.

**Ignore messages sent by any bot** (`message.from_user.is_bot`). Two bots
mentioning each other would otherwise loop.

**Removal closes sessions.** When `my_chat_member` reports the bot leaving
or being kicked, close that chat's active session with `explicit_stop`.
Without this the worker keeps trying to ask its closing question in a chat
it cannot reach, forever — the open item S8 recorded.

**Dedup before anything with side effects (R10):** `is_duplicate(update_id)`
gates every path, membership updates included.

**Tests must cover, at minimum:**
- A duplicate `update_id` reaches neither router function.
- A message from a bot is ignored.
- Dormant chat -> `handle_dormant_message`; active chat ->
  `handle_active_message`, with the session row passed through.
- The bot being added -> the greeting path, no session created.
- The bot being removed -> the chat's active session is closed with
  `explicit_stop`; a chat with no active session is a no-op.
- `main()` resolves `bot_username` via `get_me()` rather than a constant.


- [ ] Implement each task TDD-first: write the tests the spec above requires,
  watch them fail, then implement `bot/router.py` / `bot/main.py`.
- [ ] Run the full suite (184 passing on main) and commit per task.
---

## Implementation notes (added during S9, after code review)

Handed to the subagent as a specification rather than literal code — the
pre-pivot brief still had `route_update` taking a flat `text` and `chat_id`,
which the addressing gate cannot work with (it reads `entities`,
`caption_entities` and `reply_to_message`, none of which survive being
flattened to a string).

Four defects found during verification, each reproduced first.

1. **[Medium] Two simultaneous stops posted two summaries.** `close_session`
   returns a bool specifically so the loser of a race can stay quiet; the
   router ignored it. Reproduced with `asyncio.gather`: **2 messages** for one
   closure.

2. **[Medium] A late "да" to an already-closed session posted a full summary.**
   The worker's auto-close can beat a reply. `record_closing_reply`'s own
   docstring says the caller should use its return value "so it can tell the
   user the session already closed"; the caller ignored it, so the person was
   told they had closed a session the worker closed hours earlier.

3. **[Medium — R10] A confirmed destructive action reported success blindly.**
   `resolve_confirmation` returns None if the row was already resolved, and
   the delete legitimately finds nothing when the item was renamed or removed
   between proposal and confirmation. "Готово" in either case is the
   confident-but-wrong answer R10 exists to prevent.

4. **[High — R2 was unbuilt] Participants' DM replies went nowhere.** The
   subagent flagged this honestly as a gap rather than inventing routing, and
   it was right to: R2 is listed against S9 in the epic, so it was in scope. A
   DM carries no session, so a reply fell into the dormant path and tried to
   start a new session. `handle_private_message` now finds the sender among
   participants still marked unknown in any active session, records their
   answer, and returns False when the message isn't an answer to a pending
   nudge so ordinary handling still applies.

**Divergence accepted from the subagent:** `route_membership` gained
`*, update_id`. The spec's own R10 line requires dedup "membership updates
included", which its stated signature made unreachable. Correct catch.

## A production defect surfaced by a flaky test

The quiet-hours test picks whichever IANA zone is currently at the hour under
test. It failed once with
`asyncpg InvalidParameterValueError: time zone "US/Hawaii" not recognized`.

That was not just a test bug. **zoneinfo knows 599 zones; Postgres knows 487.**
The 113 extras are mostly legacy aliases — `America/Buenos_Aires`,
`US/Hawaii`, `Africa/Asmera` — exactly the names a model reaches for.
`set_chat_timezone` validated only against zoneinfo, so one of them could be
stored, and then `claim_sessions_for_closing_question` would raise for the
**whole batch**: one chat's bad zone silences the bot in every chat.

`known_to_postgres` now checks `pg_timezone_names` on write, so the two lists
can no longer disagree about a stored value.

## Known gap

`handle_private_message` resolves the sender to *some* active session where
they are an unconfirmed participant, taking the most recent when there are
several. Someone in two simultaneous outings could have the wrong one updated.
Rare enough to leave, but it wants the nudge to carry its session id — worth
doing if group DMs ever get busy.
