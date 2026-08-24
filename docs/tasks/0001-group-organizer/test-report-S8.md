# Test report: S8 — Background worker (reminder delivery + closing-question firing)

**Design:** `docs/tasks/0001-group-organizer/stories/S8-background-worker.md`
(requirements in `../EPIC.md`)
**Checked against:** branch `0001-S8-background-worker`, commits `02a44e6..HEAD`
**Date:** 2026-08-24
**Verdict:** PASS — after fixing 5 defects found during verification

## Requirement checklist

**R11 — Reminders survive a restart and are rate-limited per person:** PASS (after fixes)
- *"...a reminder is persisted to a durable queue table"* — written by S4,
  consumed here. `::test_delivers_due_reminder_and_marks_sent`.
- *"...not delivered before it is due"* — `::test_ignores_reminders_not_yet_due`.
- *"...the same person is not messaged more often than
  REMINDER_MIN_INTERVAL_HOURS"* —
  `::test_defers_when_person_was_reminded_too_recently`. Scoped to direct
  messages only after Defect 4;
  `::test_group_reminders_are_not_rate_limited` pins the corrected behavior.
- *"...a reminder with no target goes to the group chat"* —
  `::test_group_reminder_uses_chat_id_as_target`.
- Robustness the criteria did not state but R11's "durable" implies:
  `::test_one_unreachable_recipient_does_not_strand_the_rest`,
  `::test_a_reminder_that_can_never_be_delivered_stops_being_retried`,
  `::test_two_workers_do_not_deliver_the_same_reminder_twice`.

**R4 — Bot asks to close itself, with a silence safety net:** PASS (after fixes)
- *"...when the day after the event date arrives, then the bot posts a closing
  question"* — `::test_fires_closing_question_for_a_past_event`.
- *"...a session whose event date was never captured ... after 7 days idle"* —
  `::test_fires_closing_question_for_an_idle_undated_session`.
- *"...no reply again after that, then the bot auto-closes and posts a
  notice"* — `::test_auto_closes_after_the_second_unanswered_question`.
- *"...an explicit 'not yet' reply ... no further closing question until
  conditions change"* — the snooze is S3's, exercised here by
  `::test_does_not_fire_for_a_snoozed_session`.
- The failure path, added after Defect 5:
  `::test_a_question_that_could_not_be_sent_is_not_recorded_as_asked`,
  `::test_one_unreachable_chat_does_not_strand_the_others`,
  `::test_auto_close_stands_even_if_the_notice_cannot_be_sent`.

## Defects found during verification (all fixed on this branch)

Each **reproduced against a real Postgres before being fixed**. The
implementation matched the brief verbatim, so all five were defects in the
design. Detail and rationale in the story's implementation notes.

1. **[High — R11] One unreachable recipient stranded the batch.** Three due
   reminders, one send attempted, all three left `pending` and retried every
   60s forever.
2. **[High — R11] A doomed reminder never drained.** Added `attempts` and a
   `failed` status; verified retirement after 3 tries and silence on the 4th
   poll.
3. **[High] Two workers delivered the same reminder twice** — reproduced with
   `asyncio.gather`, 2 sends for 1 reminder. Now claimed with a status guard.
4. **[Medium] Group reminders were rate-limited**, contradicting the epic's
   own constraint (the limit is per *user id*) and deferring a time-critical
   group message by six hours.
5. **[High — R4] A closing question that failed to send was still recorded as
   asked.** Three due sessions, one unreachable chat: one send attempted, all
   three marked. Every one would auto-close two days later without the group
   ever being asked. Caused by my own S3 reasoning, which is now corrected in
   that function's docstring.

## QA findings beyond the stated criteria

- **[Deferred — Medium] Per-chat timezone, still open.** `remind_at` naive
  timestamps are read as UTC, so a UTC+3 group's "напомни в 9 утра" fires at
  12:00 local. This is the same debt carried since S3/S4 and is now *visible*:
  S8 is what actually delivers at the wrong time. It remains the single most
  user-visible outstanding issue and should be fixed before the bot is used in
  a real chat.
- **[For S9] An unreachable chat retries its closing question every poll**
  with no cap. The right fix is for S9 to handle `my_chat_member` removal and
  close sessions for chats the bot is no longer in — removing the cause rather
  than capping the symptom.
- **[Info] `fire_auto_closes` intentionally does not roll back a failed
  notice.** The close is correct and committed; reopening would re-close on
  the next poll. Pinned by a test so it is not "fixed" later by mistake.
- **[Checked, not a problem] A bare `telegram.Bot` outside `async with`.**
  PTB 22.8 initializes on first call — verified directly rather than assumed.

## Verification methods used

No live Telegram and no live model calls: Telegram is mocked, and every test
runs against a **real Postgres** via the `db_pool` fixture. The concurrency
defect was reproduced with genuinely concurrent coroutines against a real
database. Retry exhaustion was verified across four simulated polls, observing
`attempts` and `status` transition 1 → 2 → `failed` → untouched.
Suite: **165 passed**.
