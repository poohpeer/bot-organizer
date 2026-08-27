# Bug fix: participant names ending up on the shopping list

**Reported:** live, by the user, with a real transcript from the deployed bot
**Checked against:** branch `fix-people-as-list-items`
**Date:** 2026-08-28
**Verdict:** PASS — root cause identified, reproduced, fixed, verified live

## The report

The user pasted the bot's own "текущая информация по организации" output:

```
🛒 Список покупок:
—   арбуз — Alex
—   пиво
—   Андрюха
—   Витька

👥 Участники:
—   Андрюха (подтвердил)
—   Витька (неизвестно)
—   Alex (неизвестно)
```

Two people's own names, bare, no quantity, sitting in the shopping list next
to real items. My first read of the pasted text mistook the participants
section for the shopping list and answered wrong — the user's follow-up with
the raw text was what actually pinned it down. Worth recording: I should have
asked to see the raw message rather than answering from a guess the first
time.

## Root cause

`_silent_capture` (unaddressed messages during an active session) asks a
cheap classifier to extract `list_items` from ordinary chat, on the strength
of a description that only says "things to get/buy/bring" — nothing tells it
a person's name is not a candidate. A message shaped like "Андрюха и Витька
тоже придут" (they're coming too) is structurally close enough to "молоко и
хлеб тоже принесите" (bring milk and bread too) that a fast classifier can
extract the subject as if it were the object.

`list_add` then stored whatever name it was given with zero cross-check
against who the session already knows as a participant.

Reproduced directly against a real database, no model involved — this is a
storage-layer gap, not something that requires reproducing the classifier's
mistake:

```
list_add('Андрюха') -> {'status': 'ok', 'item_id': 1}
list_add('Витька')  -> {'status': 'ok', 'item_id': 2}
в списке покупок сейчас: ['Андрюха', 'Витька']
```

## Fix

**Code-level guard, not just a prompt fix** — consistent with how this project
has handled every other model-reliability gap (the transliteration guard in
`0002`'s S1, the duplicate-item guard in `0001`'s S6). A prompt is advisory;
the model that produced this bug already had a perfectly good `set_participant`
tool and used it correctly for the same names in the same conversation, so
instructions alone were not the failure mode to trust a fix to.

`list_add` now checks the name against `participants.display_name` for the
same session (case-insensitive, matching the project's existing convention)
before inserting, and refuses with `{"status": "looks_like_a_participant",
"detail": <name>}` rather than storing it.

This is a single choke point that protects **every** caller — both
`_silent_capture`, which never inspects `list_add`'s return value at all, and
the addressed tool-calling path, where the model sees the refusal and can
react. The instruction was extended too, in both prompts, as a second,
cheaper line of defense — but the database check is the one actually load-
bearing here.

```
list_add('Андрюха') -> {'status': 'looks_like_a_participant', 'detail': 'Андрюха'}
list_add('Витька')  -> {'status': 'looks_like_a_participant', 'detail': 'Витька'}
в списке покупок сейчас: []
```

## Edge cases checked, not assumed

- **An item added before the name became a participant is not retroactively
  removed.** The guard only stops a *new* add; deleting an existing row
  because a later participant happens to share its name would be its own
  surprising failure mode. Verified: adding "Персик" as an item, then
  recording a participant named "Персик", leaves the item in place.
- **Ordinary items are unaffected.** "арбуз" still adds cleanly in a session
  that has a participant named "Alex".
- **Case-insensitive**, matching every other name comparison in this
  codebase.

## Verification method

The database-layer fix was reproduced and verified directly, with no model
involved — the bug is in storage, not in extraction, so fixing extraction
alone (prompt-only) would have left the same gap for any other phrasing that
confuses the classifier the same way. Three regression tests were confirmed
to fail with the fix removed, including the end-to-end one through
`handle_active_message`'s unaddressed path, which is the actual path that
produced the reported bug.

Suite: **370 passed**, up from 365.

## Not fixed here: the already-corrupted production data

This branch fixes new writes; it does not touch rows that already exist. The
user's own chat currently has "Андрюха" and "Витька" as list items from
before this fix shipped. Cleaning that up needs either direct database access
(not available from this session) or asking the bot in the group to remove
them, which goes through the existing confirmation gate
(`list_remove_item` → explicit "да").
