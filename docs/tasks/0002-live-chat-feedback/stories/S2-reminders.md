# Story S2: Repeating reminders and a schedule view

**Part of:** Live-chat feedback (`../EPIC.md`)
**Goal:** A reminder can repeat until a stated boundary, and anyone can ask
what is currently scheduled.
**Satisfies:** R1, R2
**Depends on:** none
**Parallel-safe with:** none
**Requirements & global constraints:** see `../EPIC.md`

## The shape of a repeating reminder

**One row that moves, not many rows.** After a repeating reminder is delivered,
its `remind_at` advances by the interval and it stays `pending`; when the next
occurrence would fall past `repeat_until`, it becomes `sent` and stops. The
alternative — pre-creating every occurrence — makes cancelling a race (R1's
fourth criterion: cancelling once must stop all of them) and floods the table
for a "каждые 5 минут до завтра".

**The rate limit does not apply.** `worker/reminders.py` defers a direct
message when the same person had one inside `REMINDER_MIN_INTERVAL_HOURS`
(default 6). A reminder repeating every 30 minutes would be deferred forever.
That limit is there so the bot cannot pester someone unbidden, and a cadence
someone asked for out loud is not unbidden — so repeating reminders skip it,
and `REMINDER_MIN_REPEAT_MINUTES` (default 5) is the abuse floor instead. This
is a deliberate exemption; it is not an oversight, and it needs a test saying
so, or a future reader will "fix" it.

---

### Task 1: Schema

**Satisfies:** R1

**Files:**
- Modify: `db/pool.py`
- Modify: `tests/test_db_pool.py`

Add to the `reminders` block in `_SCHEMA_SQL`, and idempotently to
`_ALTERS_SQL` (the live table has rows — both columns are nullable, which is
what "not repeating" means):

```sql
ALTER TABLE reminders ADD COLUMN IF NOT EXISTS repeat_every_minutes INT;
ALTER TABLE reminders ADD COLUMN IF NOT EXISTS repeat_until TIMESTAMPTZ;
```

In `_SCHEMA_SQL` the same two columns, with a comment explaining that NULL
`repeat_every_minutes` means a one-shot reminder — the column is the flag.

**Tests must cover:** both columns exist after `init_db`; `init_db` twice in a
row still succeeds (the existing idempotency test should already cover this —
confirm it does rather than assuming).

- [ ] **Step 1–5** — test, fail, implement, green, commit.

---

### Task 2: `reminder_set` accepts a repeat

**Satisfies:** R1

**Files:**
- Modify: `bot/tools/core.py`, `bot/tools/schema.py`
- Modify: `tests/test_tools_core.py`

**Interfaces:**
- Produces: `reminder_set(pool, session_id, message, remind_at, target_user_id=None, repeat_every_minutes=None, repeat_until=None) -> dict`

Both new parameters are optional and must be added to the declaration in
`bot/tools/schema.py` — declaration and signature must match exactly, which
`tests/test_tools_composed.py::test_declared_parameters_match_the_bound_signatures`
already enforces for composed tools; check whether core tools have the same
guard and add one if not.

`repeat_until` is an ISO-8601 local time, parsed and converted exactly as
`remind_at` already is — the same `timezones.to_utc` path, so a repeat that
crosses a DST boundary lands correctly.

Return values, all of which the model sees and must be able to act on:

| Situation | Return |
|---|---|
| repeat requested, valid | `{"status": "ok", "reminder_id": n, "repeats_every_minutes": m, "repeats_until": "<local ISO>"}` |
| `repeat_every_minutes` given, `repeat_until` missing | `{"status": "repeat_needs_an_end", "detail": "..."}` — schedule **nothing** |
| interval below the floor | `{"status": "repeat_too_frequent", "minimum_minutes": 5}` — schedule nothing |
| `repeat_until` already in the past | `{"status": "repeat_end_in_the_past"}` — schedule nothing |

`repeat_needs_an_end` is R1's second criterion in mechanical form: the tool
refuses, the model has to ask. Relying on the instruction alone would make it a
suggestion.

**Tests must cover:** each row of that table, asserting no row is written for
the three refusals; a repeating reminder storing both columns; a non-repeating
call still behaving exactly as before.

- [ ] **Step 1–5.**

---

### Task 3: The worker repeats and stops

**Satisfies:** R1

**Files:**
- Modify: `worker/reminders.py`
- Modify: `tests/test_worker_reminders.py`

After a successful send, in the same transaction as the claim:

- `repeat_every_minutes IS NULL` → mark `sent`, as today.
- otherwise compute `next = remind_at + repeat_every_minutes`; if
  `next <= repeat_until`, set `remind_at = next`, keep `pending`, reset
  `attempts` to 0; if not, mark `sent`.

Advance from the **scheduled** `remind_at`, not from `now()`. A worker tick
that runs late must not drift the whole series later — over a day of
half-hourly reminders that is an hour of accumulated lag.

If a repeating send **fails**, the existing attempts/`failed` logic applies to
that occurrence unchanged; do not advance the schedule on a failure.

The per-target rate-limit check in `deliver_due_reminders` must be skipped when
`repeat_every_minutes IS NOT NULL` — see the reasoning at the top of this
story.

**Tests must cover:** a repeating reminder delivering, advancing, and
delivering again on the next poll; stopping exactly at `repeat_until` (a poll
after it delivers nothing and the row is no longer pending); a repeat every 30
minutes for the same user delivering twice in a row with
`min_interval_hours=6` set — the exemption, pinned; a cancelled repeating
reminder delivering nothing further; lateness not causing drift (set
`remind_at` well in the past and assert the next `remind_at` is the scheduled
one, not `now()+interval`).

- [ ] **Step 1–5.**

---

### Task 4: `reminder_list`

**Satisfies:** R2

**Files:**
- Modify: `bot/tools/core.py`, `bot/tools/schema.py`
- Modify: `tests/test_tools_core.py`

**Interfaces:**
- Produces: `reminder_list(pool, session_id) -> dict`

Returns `{"reminders": [...]}`, each entry:
`{"reminder_id", "message", "next_at" (local ISO, chat's timezone),
"repeats_every_minutes" (or None), "repeats_until" (local ISO or None),
"target": "группа" | display name of the target participant}`.

Only `status = 'pending'`, ordered by `remind_at`. Empty list when there is
nothing — R2's second criterion is that the bot says so plainly, which it can
only do if the tool distinguishes "none" from "cannot check".

Resolve `target_user_id` to a name via `participants` for that session; fall
back to the raw id when the person is not in the table, rather than dropping
the row.

**Tests must cover:** pending reminders listed with local times in the chat's
timezone (set a non-UTC zone and assert the rendered hour); sent and cancelled
ones absent; the empty case; a group reminder showing "группа"; a targeted one
showing the participant's name; an unknown target falling back to the id.

- [ ] **Step 1–5.**

---

### Task 5: Tell the model how to use them

**Satisfies:** R1, R2

**Files:**
- Modify: `bot/router.py` (`_ACTIVE_MODE_SYSTEM_INSTRUCTION`)
- Modify: `tests/test_router.py`

Add, in the instruction's existing English voice:

> When someone asks to be reminded repeatedly, ask how long to keep reminding
> before scheduling anything — "следующие 3 часа", "до завтра до 17:00" — and
> pass it as `repeat_until`. If `reminder_set` returns `repeat_needs_an_end`,
> `repeat_too_frequent` or `repeat_end_in_the_past`, say what is wrong and ask
> again; do not schedule a one-off instead without saying so.
> To answer "what is scheduled", call `reminder_list` — never say you have no
> way to check.

That last clause is verbatim the complaint in `bugs`: the bot said it had no
tool for this. Now it has one, and the instruction has to point at it.

**Tests must cover:** the instruction names `reminder_list` and `repeat_until`.

- [ ] **Step 1–5.**
