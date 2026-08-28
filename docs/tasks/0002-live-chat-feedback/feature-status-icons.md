# Feature: status icons for the shopping list and participant roster

**Requested:** live, by the user, with an exact icon scheme
**Checked against:** branch `status-icons`
**Date:** 2026-08-28
**Verdict:** PASS

## What was asked

Shopping list: `✅` if someone has claimed an item, `◻️` if nobody has.
Participants: `✅` confirmed, `◻️` no response yet, `❓` unsure, `❌` not coming.
Icons must stay current as status changes.

## A gap the request surfaced

The participant scheme names **four** states; the schema had **three**
(`unknown`/`confirmed`/`declined`). "❓ не уверен" (unsure) is genuinely
distinct from "◻️ пока не ответил" (no response at all) — someone who replies
"может быть" has answered, just non-committally, which is a different fact
than silence. Added a fourth status, `maybe`, via the same idempotent
widen-the-CHECK-constraint pattern already used for `reminders.status`
(`0001`'s worker story).

## A gap the request also surfaced, unprompted

There was no code path enforcing consistent shopping-list formatting on the
model at all — `list_show` has returned a deterministic `rendered` field since
`0001`'s S3, but nothing ever told the model to use it verbatim. The pasted
transcript that prompted this request shows exactly that: a free-composed
report with its own bullet style (`—`), not `list_render.render()`'s output.
Adding icons to the renderer alone would not have made them appear reliably —
the model could keep reformatting around them. Fixed by adding an explicit
instruction: use `list_show`'s and `get_participants`' `rendered` fields
character-for-character, do not reformat, do not write a status in words. This
is the same reasoning applied everywhere else in this project when a rule
depends on the model's cooperation (the transliteration guard, the "empty
string" sentinel): make it maximally easy for the model to comply verbatim
rather than trusting it to reconstruct a rule from a sentence.

## What was built

- `bot/list_render.py`: item lines now lead with `✅`/`◻️` instead of `—`,
  based on `claimed_by`. Neither icon is `*`, `-` or `+` at a line start, so
  `bot.formatting.to_plain_text`'s bullet rewrite still never touches these
  lines — verified directly, not assumed.
- `bot/participant_render.py` (new): one line per participant, `{icon}
  {display_name}`, icon from status. An unrecognised status falls back to
  `◻️` rather than raising — a row from before some status existed, or a
  future addition, must still render as *something*.
- `get_participants` gains a `rendered` field, matching `list_show`'s existing
  contract, and now orders by `id` (previously unordered — harmless before
  since nothing depended on order, but worth being explicit rather than
  relying on incidental row order).
- `participants.status` CHECK widened to include `maybe`; the tool
  declaration's description explains when to use it.
- `nudge_unconfirmed_participants` stays scoped to `unknown` only —
  documented explicitly why: someone who hedged has already answered, and
  re-nudging them chases a response they already gave.

## "Update the icons when a status changes"

Not a separate mechanism. Both renderers are pure functions of the rows
handed to them — there is no cached rendering anywhere — so the requirement
falls out of calling `render()` again, which every `list_show`/
`get_participants` call already does. Verified end to end against a real
database: recorded a participant as `unknown`, rendered (`◻️`), changed their
status to `confirmed`, rendered again (`✅`) — same session, same renderer
call site, different output, nothing to keep in sync by hand.

## Verified live, exactly the reported scenario

```
🛒 Список покупок:
Ещё не разобрали:
Прочее
◻️ пиво

Уже взяли:
Прочее
✅ арбуз — Alex

👥 Участники:
✅ Андрюха
◻️ Витька
❓ Alex
❌ Марина
```

Both `rendered` strings pass through `to_plain_text` byte-for-byte unchanged —
the guard that keeps S1 and this feature from fighting each other.

## Verification method

The icon-mapping tests were confirmed to fail when a status/icon pairing was
removed (`assert '✅ Андрюха\n◻...Света\n❌ Alex' == ...`). Every behaviour ran
against a real PostgreSQL — no mocked repositories. Suite: **378 passed**, up
from 370.

## Not verified live

Whether the model actually reproduces `rendered` character-for-character
rather than paraphrasing it, given the instruction is advisory the same way
every other prompt-level rule in this project is. This is the one thing here
that cannot be settled by a unit test — it needs a real chat, which is exactly
what the reported bug came from in the first place.
