# Story S3: Quantity, ownership and order on the shopping list

**Part of:** Live-chat feedback (`../EPIC.md`)
**Goal:** The list shows quantity and who is bringing each item, grouped so it
reads at a glance — as plain text.
**Satisfies:** R4
**Depends on:** none
**Parallel-safe with:** none
**Requirements & global constraints:** see `../EPIC.md`

## Two decisions worth reading before starting

**No table, and no `parse_mode`.** An aligned table needs a monospace block,
which needs HTML, which means every `<` in an item name has to be escaped or
Telegram drops the whole message — a worse failure than ragged columns, since
the group would see nothing at all. The list is therefore rendered as grouped
plain text, which reads correctly in any font and cannot fail to send. This
also keeps the bot to exactly one outgoing format, which S1 already
guarantees.

**The category vocabulary is fixed and lives in code, not in the model's
head.** Sorting has to be stable across calls, and a model asked to invent
categories will say "молочка" once and "молочные продукты" the next time, which
sorts differently and looks broken. The tool takes a category from a closed
list and falls back to `прочее` for anything else — including anything the
model makes up.

```
мясо, молочка, овощи и фрукты, напитки, хлеб и выпечка,
бакалея, посуда, прочее
```

---

### Task 1: Schema

**Satisfies:** R4

**Files:**
- Modify: `db/pool.py`
- Modify: `tests/test_db_pool.py`

```sql
ALTER TABLE list_items ADD COLUMN IF NOT EXISTS quantity TEXT;
ALTER TABLE list_items ADD COLUMN IF NOT EXISTS category TEXT;
ALTER TABLE list_items ADD COLUMN IF NOT EXISTS claimed_by TEXT;
ALTER TABLE list_items ADD COLUMN IF NOT EXISTS claimed_by_user_id BIGINT;
```

Same two places as always — `_SCHEMA_SQL` and `_ALTERS_SQL`. All nullable:
NULL `quantity` is R4's "не указали сколько — не пиши ничего", and NULL
`claimed_by` is "nobody has taken it".

`quantity` is **TEXT, not a number.** People say "пару бутылок", "кг", "штук
5". Parsing that into a number and a unit invents precision nobody gave, and
R4 only asks to show what was said.

`claimed_by` and `claimed_by_user_id` are both kept for the same reason
`participants` keeps both: someone can be named before they are identified.

**Tests must cover:** all four columns exist after `init_db`; existing rows
survive the ALTER with NULLs (insert a row, run `init_db` again, assert it is
still there).

- [ ] **Step 1–5.**

---

### Task 2: Recording quantity, category and who

**Satisfies:** R4

**Files:**
- Modify: `bot/tools/core.py`, `bot/tools/schema.py`
- Modify: `tests/test_tools_core.py`

**Interfaces:**
- `list_add(pool, session_id, name, quantity=None, category=None) -> dict`
  — existing signature plus two optionals, so an add with neither still works.
- `list_claim(pool, session_id, name, claimed_by, claimed_by_user_id=None) -> dict` — new.
- `list_unclaim(pool, session_id, name) -> dict` — new.

`list_add` keeps its existing idempotency exactly as it is (`0001`'s S6): a
second add of the same name returns `already_present` and does not duplicate.
When an add carries a quantity for an item that already exists **without** one,
fill it in and return `{"status": "updated", ...}` — someone saying "картошки,
килограмма два" after "возьмите картошки" is adding information, not repeating
themselves. Never overwrite a quantity that is already set; return
`already_present` with the existing value so the model can mention it.

`list_claim` matches the item by name the same way `list_check_off` does, and
returns the same shape of miss: `{"status": "not_found", "items_on_the_list":
[...]}`. That affordance exists because a model once invented an item rather
than matching one (`0001`'s S6) — the same failure applies here.

`list_unclaim` clears both claim columns. R4's third criterion: the item goes
back among the unclaimed, so it must also move back to the top of the table,
which falls out of the sort in Task 3.

An unknown category — including one the model invents — is stored as `прочее`,
not rejected. A refusal here would cost a whole turn to fix a cosmetic detail.

**Tests must cover:** adding with and without a quantity; a quantity filling in
a blank one; a quantity **not** overwriting an existing one; claiming; claiming
an item that is not there returning the real names; unclaiming; an invented
category landing in `прочее`; the existing idempotency tests still passing
unchanged.

- [ ] **Step 1–5.**

---

### Task 3: Rendering the list

**Satisfies:** R4

**Files:**
- Create: `bot/list_render.py`
- Modify: `bot/tools/core.py` (`list_show`)
- Create: `tests/test_list_render.py`

**Interfaces:**
- Produces: `bot.list_render.render(items: list[dict]) -> str`
- `list_show` returns `{"items": [...], "rendered": "<text>"}` — the rows stay
  in the response so the model can reason about them, and `rendered` is what it
  should show.

**Order**, in this precedence — R4's fourth criterion:

1. unclaimed before claimed
2. then by category, in the fixed vocabulary's order (not alphabetically —
   `мясо` before `молочка` because that is the order people shop in)
3. then by item name, alphabetically, case-insensitively

Cyrillic and Latin names sort together under `str.lower()`; do not reach for a
locale-aware collator, which drags in a dependency for a cosmetic gain.

**Shape.** Two sections, category headings inside each, one line per item.
Quantity after a comma when there is one; the person after an em dash when
somebody has taken it:

```
Ещё не разобрали:
Хлеб и выпечка
— хлеб
Молочка
— молоко, 2 л
Посуда
— бумажные тарелки

Уже взяли:
Напитки
— вода, 6 бутылок — Alex
```

A section that would be empty is omitted entirely, heading and all — a list
where nobody has taken anything must not end with a bare "Уже взяли:".

Item lines start with an em dash, which S1's converter leaves alone (it only
rewrites `*`, `-` and `+` at a line start). Do not switch to `-`, or the
converter will rewrite lines this module already formatted.

**Tests must cover:** the three sort keys exercised at once (build a list that
comes out differently if any one is dropped or reordered); an empty list
rendering something a human can read rather than an empty string; a missing
quantity rendering with no trailing comma and never the word `None`; a claimed
item showing the name; the claimed section omitted when nothing is claimed and
the unclaimed section omitted when everything is; an item name containing `<`,
`*` or `_` surviving verbatim — there is no markup here to escape, and
mangling it would be a bug; the rendered output passing through
`bot.formatting.to_plain_text` unchanged, which is the guard that S1 and S3
cannot fight each other.

- [ ] **Step 1–5.**

---

### Task 4: Tell the model about quantity and ownership

**Satisfies:** R4

**Files:**
- Modify: `bot/router.py` (`_ACTIVE_MODE_SYSTEM_INSTRUCTION`)
- Modify: `tests/test_router.py`

Add:

> When someone names an amount, pass it to `list_add` as `quantity`, in their
> words — "пару бутылок", "кг". Never invent an amount that was not said.
> When someone says they will bring something, call `list_claim` with their
> name; if they say they cannot after all, call `list_unclaim`. Categorise each
> item with one of: мясо, молочка, овощи и фрукты, напитки, хлеб и выпечка,
> бакалея, посуда, прочее.

**Tests must cover:** the instruction names `list_claim`, `list_unclaim` and
the category vocabulary, and forbids inventing amounts.

- [ ] **Step 1–5.**
