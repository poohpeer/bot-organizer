# Test report: S2 — Repeating reminders and a schedule view

**Design:** `docs/tasks/0002-live-chat-feedback/stories/S2-reminders.md`
**Checked against:** branch `0002-S2-reminders`
**Date:** 2026-08-27
**Verdict:** PASS — after fixing one defect the story specified into existence

## Requirement checklist

**R1 — Reminders that repeat until told to stop:** PASS
- *"repeats at that interval and stops at that boundary"* — verified against a
  real database: a half-hourly repeat delivers, advances exactly 30 minutes,
  delivers again, and stops once the next slot passes `repeat_until`.
- *"asks how long instead of guessing"* — enforced by the tool, not by the
  instruction: `repeat_needs_an_end` is returned and **no row is written**.
  Confirmed by counting rows after the call, not by reading the status.
- *"cancelling once stops all future repeats"* — one row that moves, so cancel
  is the existing single-row update. Verified: zero deliveries after cancel.
- *"an interval below the floor is refused with the minimum stated"* —
  `repeat_too_frequent` with `minimum_minutes`, nothing written.
- The rate-limit exemption is pinned by a test that seeds an unrelated recent
  DM to the same person, so the exemption is exercised rather than merely
  absent by coincidence. Verified live: two consecutive deliveries with
  `min_interval_hours=6`.

**R2 — See what is scheduled:** PASS
- Pending only, ordered, times rendered in the chat's timezone, group vs.
  named target resolved through `participants` with an id fallback.
- The empty case returns an empty list rather than an error — R2's second
  criterion is that the bot can say "nothing is scheduled", which it can only
  do if the tool distinguishes none from cannot-check. That distinction is the
  whole point: the complaint in `bugs` was the bot claiming it had no way to
  look.

## Defect found during verification

**A badly overdue repeating reminder delivered one missed copy per poll,
forever, until it caught up.**

The story said to advance by exactly one interval, and the implementation did
exactly that. Correct for the normal case, and wrong the moment anything falls
behind:

| Situation | Messages, one per minute |
|---|---|
| worker down 1 hour, repeat every 5 min | 12 |
| worker down 1 day, repeat hourly | 24 |
| reminder mis-dated to 2025-07-20, repeat every 30 min | **19 352 — thirteen days** |

That last row is not hypothetical. It is the `bugs` file's own incident: the
model stored `2025-07-20` for "напомни через 5 минут". Fixed at the time by
giving the model the current date, but a mis-dated repeat would have been far
louder than a mis-dated one-shot, and nothing stops the model getting a date
wrong again.

Fixed by skipping missed occurrences rather than queueing them: the reminder
fires once and the schedule jumps to the next future slot. Both properties now
hold at once — a tick that is seconds or minutes late still steps exactly one
interval (no drift), while an hour-long outage produces one message and a next
slot five minutes out.

The story has been corrected, with the measurements, so the next reader does
not restore the single-interval step.

## QA notes

- **The rate-limit exemption is deliberate and now documented in three
  places** — the epic, the story and a test whose name says so. It is exactly
  the kind of thing that looks like a bug to someone reading
  `worker/reminders.py` cold.
- **`reminder_list` on an unknown `session_id` returns an empty list**, not an
  error — matching `list_show`'s existing precedent for read-only tools. Worth
  knowing: a hallucinated session id looks the same as an empty schedule.
- **Not verified live.** Repeating delivery has been exercised against a real
  database with Telegram mocked, but never against Telegram itself — the test
  group was deleted. What a repeat looks like arriving in a real chat, and
  whether the model reliably asks for a duration rather than defaulting to a
  one-shot, both still need a live run.

## Verification method

Every behaviour above was exercised against a real PostgreSQL; Telegram and the
model are the only mocked boundaries. The three regression tests for the
catch-up defect were confirmed to fail with the fix removed — including the
end-to-end one, which failed `assert 5 == 1`: five polls, five messages.

Suite: **300 passed**, up from 275.
