# Test report: S5 — Group title and description sync

**Design:** `docs/tasks/0002-live-chat-feedback/stories/S5-group-sync.md`
**Checked against:** branch `0002-S5-group-sync`
**Date:** 2026-08-27
**Verdict:** PASS — after fixing 1 defect found during verification

Implemented across two sessions: a subagent completed Tasks 1–5 and started
Task 6 before hitting a session limit; Task 6 was finished and the whole
story verified in this session.

## Requirement checklist

**R7 — Follow the group's own title and description:** PASS

- *"greets with what it can see, and still starts no session"* — verified: the
  greeting reports activity/date/place and the member count as a number, and
  no session row is created either with or without extractable info.
- *"a member count, never a roster"* — the sentence is built from
  `get_chat_member_count` and never lists names; existing tests plus code
  inspection confirm no name list is assembled anywhere in the greeting path.
- *"the worker posts what changed, once per change"* — verified against a real
  database across five consecutive polls after one real change: `announced`
  was `[1, 0, 0, 0, 0]`, one message sent total.
- *"nothing changed, the worker says nothing"* — same run, four silent polls.
- *"a dormant chat is never even fetched"* — verified with a spy Telegram
  client that raises on any call: zero calls for a chat with no active
  session.
- *"nothing invented from a bare title"* — `extract_event` fails closed to
  all-None, inherited from `bot.ai.classify.extract`'s existing contract.
- *"states the roster gap only when the numbers differ, never when the count
  is unknown"* — verified: no stored count returns `None` (never `0`); equal
  counts carry no remark condition; a real gap (1 recorded vs. 9 in the chat)
  is available for the instruction to act on.

## Defect found during verification

**A rejoin silently overwrote an already-confirmed participant back to
`"unknown"`, and the bot then announced it was waiting for a confirmation
that had already been given.**

`handle_new_members` called `set_participant(..., "unknown", ...)`
unconditionally for every name in `new_chat_members` — including someone
already recorded. `set_participant` (from `0001`'s S4) matches by `user_id`
and *updates* the existing row rather than inserting a second one, so no
duplicate appeared and the story's own duplication test passed cleanly. But
the update carries the fixed `"unknown"` status, which clobbered whatever was
there.

Reproduced against a real database: a participant recorded as `confirmed`,
then a `new_chat_members` event for the same `user_id` —

```
статус до 'повторного вступления': 'confirmed'
статус после: 'unknown'   !!! ЗАТЁРТО !!!
сообщение группе: 'У нас новый участник — Света. Я добавил его в список,
                    жду подтверждения.'
```

Telegram redelivers `new_chat_members` on an actual leave-and-return, and
there is no guarantee it never fires more than once for the same event. Either
way the effect is the same: real confirmation data destroyed, and the group
told something false about someone who had already answered — this is a
regression against `0001`'s R2, introduced by a story that never touches R2's
own files.

Fixed with `get_participant_status`, checked before `set_participant` is
called: a `user_id` already on record is treated as a rejoin and skipped
entirely — no status write, no announcement. Verified live after the fix: the
`confirmed` status survives, and zero messages are sent for the rejoin.

The story's own criterion said only "not duplicating them"; it has been
corrected to name the status-preservation requirement explicitly, since the
duplication test alone did not catch this.

## QA notes

- **`chats.member_count` is read from a column updated on a timer**
  (`GROUP_SYNC_INTERVAL_SECONDS`, default 60), not fetched live when someone
  asks about participants. A value up to a minute stale is the stated
  trade-off for keeping an ordinary question off the network path — worth
  remembering if a group's membership changes right as someone asks.
- **A join is filtered out of `route_update` before the addressing gate or
  dedup-adjacent handling that ordinary messages get** — it is a service
  message, not something to run through the model. This is correct and
  matches how the router already treats other non-conversational updates.

## Verification method

Every property was exercised against a real PostgreSQL: first-sighting
suppression, the once-per-change guarantee across repeated polls, the dormant
chat never being fetched (proven with a spy client, not merely absent
messages), and the roster-gap counts. The rejoin defect was reproduced with
concrete before/after output, and the regression test was confirmed to fail
with the fix removed (`assert 'unknown' == 'confirmed'`).

Suite: **365 passed**, up from 335.

## Not verified live

Whether `extract_event` reliably resolves a bare date like "4 июля" against
the chat's actual timezone in practice, and how the enriched greeting reads in
a real Telegram client — the test group was deleted. Both are the kind of
thing that looks right in a unit test and still needs a real chat to trust.
