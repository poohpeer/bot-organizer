# Test report: S3 — Quantity, ownership and order on the shopping list

**Design:** `docs/tasks/0002-live-chat-feedback/stories/S3-list-details.md`
**Checked against:** branch `0002-S3-list-details`
**Date:** 2026-08-27
**Verdict:** PASS

## Requirement checklist

**R4 — The shopping list with quantity, owner and order:** PASS. Every
criterion was exercised against a real database, not read off the code:

- *"an amount that was not given stays empty"* — `list_add(name)` with no
  quantity stores NULL and renders with no trailing comma and no `None`.
- *"someone's name against the item they are bringing"* — `list_claim` stores
  it, and the rendered line ends `— Света`.
- *"cancelling puts the item back among the unclaimed"* — `list_unclaim`
  clears both claim columns; verified the column is NULL afterwards, and the
  item moves back to the first section, which falls out of the sort.
- *"unclaimed first, then by category, then alphabetically"* — verified with a
  list built so that dropping or reordering any one key changes the output.
  Case-insensitivity confirmed: `Абрикосы` before `ананас`.
- *"plain text, nothing that depends on a font"* — no `parse_mode` anywhere,
  and the rendered output passes through `bot.formatting.to_plain_text`
  unchanged, which is the guard that S1 and S3 cannot fight each other.

Also verified beyond the criteria: a quantity fills in a blank one
(`updated`) but **never overwrites** an existing one (`already_present`, and
the stored value is unchanged); an invented category lands in `прочее` rather
than being refused; `list_claim` on a name that is not there returns the real
names, the same affordance `list_check_off` has; and `list_add`'s existing
idempotency is intact — no duplicate rows.

## A defect in this document, found by the implementer

The story stated the category rule twice ("vocabulary order, not
alphabetical — мясо before молочка") and then showed a worked example with
`Хлеб и выпечка` above `Молочка`, which is the reverse. The implementer
followed the stated rule, flagged the contradiction rather than quietly
picking one, and was right to. The example is now corrected, with a line
saying which of the two won and why — a reader who trusted the picture would
have built the wrong sort.

## Known limitation, accepted rather than fixed

An item name **entirely wrapped in paired Markdown markers** loses them on the
way out: an item literally called `*звёздочка*` displays as `звёздочка`, and
`__жирный__` as `жирный`. S1's converter cannot tell a marker the model wrote
from one that is part of a name.

Everything else survives — measured, not assumed: `2*2`, `цена 5*`, `a_b_c`,
`<тег>` and a leading `- ` all render verbatim.

Not fixed because every fix costs more than the defect: keeping the list out
of the converter needs the sentinel machinery this story deliberately removed,
and escaping would make the stored name differ from the displayed one. Item
names come from the model extracting what people said, and "добавь звёздочки"
yields `звёздочки`, not `*звёздочки*`.

## What changed from the original design

The table is gone, at the user's request, and with it the whole HTML-sending
task. That removes the failure mode that worried this story most: Telegram
rejects a message with malformed entities **entirely**, so a single unescaped
`<` in an item name would have meant the group seeing nothing at all. Grouped
plain text cannot fail to send and reads the same in any font.

## Verification method

Sorting and rendering are pure functions and were probed directly, including
the `to_plain_text` round-trip and seven awkward item names. Every tool
behaviour ran against a real PostgreSQL. Suite: **324 passed**, up from 300.

Not verified live: how the list reads in a real Telegram client, and whether
the model reliably passes a quantity in the user's own words rather than
normalising it. Both need a real chat.
