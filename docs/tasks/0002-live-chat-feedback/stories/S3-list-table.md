# Story S3: The shopping list as a table

**Part of:** Live-chat feedback (`../EPIC.md`)
**Goal:** The list shows quantity and who is bringing each item, sorted so it
reads at a glance.
**Satisfies:** R4
**Depends on:** none
**Parallel-safe with:** none
**Requirements & global constraints:** see `../EPIC.md`

## Two decisions worth reading before starting

**The table is sent as monospace, via `parse_mode="HTML"` and `<pre>`, for
that one message only.** Telegram renders plain text in a proportional font, so
space-padded columns do not line up and R4's last criterion fails. `<pre>`
fixes it. The cost is that `<`, `>` and `&` in item names must be escaped with
`html.escape` — miss it and Telegram rejects the whole message. Everything else
the bot sends stays plain text; this is the single exception and S1's converter
still applies to the model's prose around it.

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

### Task 3: Rendering the table

**Satisfies:** R4

**Files:**
- Create: `bot/list_table.py`
- Modify: `bot/tools/core.py` (`list_show`)
- Create: `tests/test_list_table.py`

**Interfaces:**
- Produces: `bot.list_table.render(items: list[dict]) -> str`
- `list_show` returns `{"items": [...], "table": "<rendered>"}` — the rows stay
  in the response so the model can reason about them, and the table is what
  gets shown.

**Sort order**, in this precedence — R4's fourth criterion:

1. unclaimed before claimed
2. then by category, in the fixed vocabulary's order (not alphabetically —
   `мясо` before `молочка` because that is the list's order, and the list is
   ordered by how people shop)
3. then by item name, alphabetically, case-insensitively

Cyrillic and Latin names sort together under `str.lower()`; do not reach for a
locale-aware collator, which would drag in a dependency for a cosmetic gain.

**Layout**, from the `bugs` file's own sketch — a leading mark column for
claimed items, then name, quantity, who:

```
 v | paper plates    | 2 упак | Alex
   | хлеб            |        |
```

Columns are padded to the widest cell in each. A `checked` item (someone
already bought it) shows `v` too — from the group's point of view it is
handled either way.

**Tests must cover:** the sort across all three keys at once (build a list that
would come out differently if any key were dropped or reordered); an empty list
rendering something a human can read rather than an empty string or a bare
header; a missing quantity rendering as blank, never `None`; a very long item
name not breaking the columns; a name containing `<` surviving (this is the
`html.escape` boundary — assert the escaping happens where the message is
built, in Task 4, and that `render` itself returns the raw name).

- [ ] **Step 1–5.**

---

### Task 4: Sending it as a table

**Satisfies:** R4

**Files:**
- Modify: `bot/router.py`
- Modify: `tests/test_router.py`

The table needs `parse_mode="HTML"` with the rendered table inside `<pre>` and
`html.escape` applied to its contents. Everything else keeps going out as plain
text.

The model receives the table in the tool result and will normally include it in
its reply. The router must recognise that the reply contains the table and send
that message with the HTML parse mode. Concretely: have `list_show` mark the
table with a sentinel the router can find and replace — the mechanism is the
implementer's to choose, but it must survive the model reflowing the prose
around it, and it must degrade to plain text rather than dropping the message
if the mark is absent.

**A failure here is worse than ragged columns**: Telegram rejects a message
with malformed HTML entirely, so the group would see nothing at all. Wrap the
HTML send and fall back to a plain-text send on `BadRequest`.

**Tests must cover:** a reply containing the table going out with
`parse_mode="HTML"`; an ordinary reply going out with no parse mode; an item
name containing `<` arriving escaped; Telegram rejecting the HTML message
falling back to a plain send rather than losing it.

- [ ] **Step 1–5.**

---

### Task 5: Tell the model about quantity and ownership

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
