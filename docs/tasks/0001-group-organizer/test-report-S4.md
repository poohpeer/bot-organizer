# Test report: S4 — Core tools (facts, lists, participants, reminders, confirmation gate)

**Design:** `docs/tasks/0001-group-organizer/stories/S4-core-tools.md`
(requirements in `../EPIC.md`)
**Checked against:** branch `0001-S4-core-tools`, commits `806c251..HEAD`
**Date:** 2026-08-24
**Verdict:** PASS — after fixing 8 defects found during verification

## Requirement checklist

**R1 — Natural-language shared list, no commands:** PASS
Verified by **driving the real tool loop against the live Gemini API** with
the idea doc's own opening scenario, not by unit tests alone:
- *"...when someone writes a message naming items to get, then the bot adds
  each item without announcing it"* — "надо купить помидоры, огурцы и мясо"
  produced exactly three pending rows (`помидоры`, `огурцы`, `мясо`) and an
  empty reply (silent capture).
- *"...when someone asks what's on the list, then the bot replies with the
  current list, showing outstanding vs checked off"* — "слушай, что там по
  покупкам?" returned "Огурцы — куплено ✅ / Помидоры — ещё нужно купить ⏳ /
  Мясо — ещё нужно купить ⏳".
- *"...when someone says they already have it, then that item is checked off,
  **matched by name even if phrased differently than when it was added**"* —
  this was the criterion I most doubted, since matching is exact on
  `lower(name)` and Russian is heavily inflected. Tested deliberately:
  "огурцы я уже взял" checked off `огурцы`, and the harder diminutive
  "помидорки уже купили" still checked off the stored `помидоры`. The model
  normalizes the `name` argument to the stored form, so exact DB matching is
  sufficient in practice. Worth re-testing if the model tier is ever
  downgraded.
- Unit coverage for the same paths, plus
  `test_list_check_off_distinguishes_already_checked_from_absent` (see
  Defect 5).

**R2 — Participant confirmation tracking + private nudges:** PASS (after fixes)
- *"...the bot reports confirmed/declined/no-response per participant"* —
  `test_tools_core.py::test_set_participant_inserts_then_updates`,
  `::test_get_participants...`.
- *"...the bot sends each unconfirmed participant a private direct message —
  never a message in the group chat naming them individually"* —
  `::test_nudge_unconfirmed_dms_only_those_with_known_user_id` confirms DMs go
  to `chat_id=<user_id>`, and that participants with no known `user_id` are
  reported as skipped rather than named in the group.
- *"...when a participant replies in DM, their status is recorded and
  reflected in the next summary"* — this **failed** before the fix (Defect 1);
  now `::test_set_participant_merges_name_mention_with_later_dm_reply`.

**R6 (S4's share) — fact storage/retrieval:** PASS
- `remember_fact`/`get_facts` with latest-value-wins semantics
  (`DISTINCT ON (key) ... ORDER BY created_at DESC`), filtered and unfiltered,
  and empty-result handling — 4 tests. The composition behavior these feed was
  already verified live in S2's report.

**R10 (S4's share) — destructive actions need explicit confirmation:** PASS (after fixes)
- *"...when the model proposes a destructive tool call, the bot asks for
  explicit human confirmation before executing — never auto-executed"* —
  verified **live**: "удали колбасу из списка" produced
  "Пожалуйста, подтвердите: вы действительно хотите удалить «колбаса» из
  списка?", the item remained on the list, and a `pending_confirmations` row
  was filed. Nothing was deleted without a human "yes".
- `broadcast_message` is gated the same way
  (`::test_broadcast_message_is_gated`).
- Replay safety was **broken** before the fix (Defect 3); now
  `::test_confirmed_action_cannot_be_executed_twice` and
  `::test_execute_confirmed_action_refuses_an_unconfirmed_row`.

**R11 (S4's share) — reminders persisted to a durable queue:** PASS
- *"...when a reminder is created, it is persisted to a durable queue table,
  not held only in the bot process's memory"* — `::test_reminder_set_persists_to_queue`
  asserts the `reminders` row with `status='pending'`. Delivery itself is S8.
- `reminder_cancel` covered, including the cross-session guard (Defect 4).

## Defects found during verification (all fixed on this branch)

Raised by `code-review:code-review`, each **reproduced against a real database
before fixing**. The implementation matched the design brief verbatim, so
these were defects in my design, not transcription errors.

1. **[High — broke R2] Duplicate participant rows.** Identity matched on
   `user_id` OR name in mutually exclusive branches. Reproduced: "Masha is
   coming" (no id) followed by Masha's DM reply (id 333) produced **two rows**
   — `[{user_id: None, 'Masha', 'unknown'}, {user_id: 333, 'Masha',
   'confirmed'}]`. `get_participants` would report her twice with conflicting
   statuses, and the nudge would DM someone who had already confirmed. Fixed
   with match-by-id → fall back to name → backfill the id.
2. **[High — broke R2 in the common case] Nudge aborted on the first
   failure.** Telegram raises `Forbidden` when DMing anyone who has never
   started a private chat with the bot — which is the *normal* state for most
   group members. Reproduced: one refusal aborted the entire run, so
   already-sent DMs went unrecorded and a retry would re-DM them. Each send is
   now isolated, with a `failed_to_reach` bucket so the bot can say who it
   couldn't reach.
3. **[Medium — broke R10] Gated action could execute twice.**
   `resolve_confirmation` had no `status = 'pending'` guard. Reproduced: two
   "yes" resolutions both succeeded and the broadcast sent twice. Guarded, and
   `execute_confirmed_action` now refuses any row not in `confirmed` state.
4. **[Medium] Cross-chat writes.** `reminder_set` and `broadcast_message` took
   `chat_id` as a *model-supplied* argument, so a hallucinated value would
   file the confirmation against another chat and post the text there.
   `reminder_cancel` was scoped only by a model-supplied global `reminder_id`.
   All three now derive/scope from `session_id`, and `chat_id` was removed
   from the tool declarations entirely.
5. **[Medium] `already_checked` indistinguishable from `not_found`.** Two
   people both saying "I got the cucumbers" would be told cucumbers aren't on
   the list.
6. **[Low] Delete over-reported and over-reached.** The confirmed delete
   claimed success even when nothing matched, and an unbounded `DELETE`
   removed *both* rows when an item had been added twice. Now single-row and
   reports `not_found` honestly.
7. **[Low] `TypeError` on an unknown `session_id`.** A stale or hallucinated
   id surfaced to the model as `'NoneType' object is not subscriptable`. Tools
   now return `unknown_session`; `reminder_set` returns `bad_datetime` rather
   than raising on unparseable input.
8. **[Low] Confirmation prompt leaked a raw params dict** into text relayed
   verbatim into the group chat (`(broadcast_message: {'text': ...})`). Now a
   sentence.

## QA findings beyond the stated criteria

- **[Deferred — Medium] Per-chat timezone.** `reminder_set` reads a naive
  ISO-8601 timestamp as UTC, but the model will normally render "remind us at
  9am" as naive local wall-clock, so a UTC+3 group's reminder fires 3 hours
  late. Same root cause as S3's deferred `event_date` issue — there is no
  per-chat timezone in the schema. **These two should be fixed together**; it
  is the single most user-visible piece of debt carried so far.
- **[Deferred — Low] Expired confirmations never marked.**
  `get_pending_confirmation` hides rows older than a day, but nothing writes
  the `'expired'` status the schema defines, so stale rows sit as `'pending'`
  forever. Harmless while the gate filters by age; needs a sweeper before
  anything queries by status alone.
- **[Info] `nudge` reaches only members who have started the bot.** Not a code
  defect but a Telegram platform constraint worth stating plainly: the bot
  cannot DM a group member who has never opened a private chat with it. The
  `failed_to_reach` bucket makes this visible, but R2's "nudge the silent
  ones" will in practice only reach a subset. S9 should surface that honestly
  in chat rather than implying everyone was messaged.

## Verification methods used

The headline R1 and R10 criteria were **run end-to-end against the live Gemini
API** driving the real tool loop against a real Postgres — including the
inflection edge case and the destructive-delete gate. R2/R6/R11 are covered by
the 32-test `test_tools_core.py` module, every test hitting a real database
via the `db_pool` fixture (no mocked DB anywhere; Telegram is the only mocked
boundary). All 8 defects were reproduced with failing assertions before being
fixed and are locked in by regression tests. A signature cross-check confirms
all 12 core tools' declarations in `bot/tools/schema.py` exactly match their
Python signatures after the `chat_id` removal. Suite: **82 passed**.
