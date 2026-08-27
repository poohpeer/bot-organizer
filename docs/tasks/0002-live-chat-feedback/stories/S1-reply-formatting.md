# Story S1: Plain-text Russian replies

**Part of:** Live-chat feedback (`../EPIC.md`)
**Goal:** Every message the bot posts reads as plain Russian text — no raw
Markdown asterisks, no drift into English.
**Satisfies:** R3, R6
**Depends on:** none
**Parallel-safe with:** none (touches `bot/router.py`, as most stories do)
**Requirements & global constraints:** see `../EPIC.md`

## Why the asterisks appear

`parse_mode` is never set anywhere in `bot/`, so every message goes to Telegram
as plain text. The model writes Markdown because that is what models do, and
Telegram renders it literally. Two ways out:

1. Set `parse_mode="Markdown"` so it renders. **Rejected.** Telegram rejects
   the whole message when entities are unbalanced, so one stray `*` in an item
   name loses the reply entirely — and item names come from users.
2. Convert the Markdown to plain text before sending. **Chosen**, and it is
   what the `bugs` file asks for: keep the lists, turn their markers into
   dashes, drop the rest.

---

### Task 1: A formatter for outgoing text

**Satisfies:** R3

**Files:**
- Create: `bot/formatting.py`
- Create: `tests/test_formatting.py`

**Interfaces:**
- Produces: `bot.formatting.to_plain_text(text: str) -> str`

**Behaviour, exactly:**

| Input | Output | Why |
|---|---|---|
| `**Куда:** Ben Shemen` | `Куда: Ben Shemen` | bold markers removed, content kept |
| `*Куда:* Ben Shemen` | `Куда: Ben Shemen` | single-asterisk emphasis too |
| `__Куда:__ x` / `_x_` | `Куда: x` / `x` | underscore emphasis, same reasoning |
| `* хлеб` (line start) | `— хлеб` | a list marker becomes a dash |
| `- хлеб` / `+ хлеб` (line start) | `— хлеб` | same |
| `  * хлеб` (indented) | `  — хлеб` | indentation is preserved |
| `1. хлеб` | `1. хлеб` | numbered lists are already readable |
| `### Основное` | `Основное` | heading markers removed |
| `` `code` `` | `code` | backticks removed |
| `2 * 3 = 6` | `2 * 3 = 6` | a lone asterisk with spaces is arithmetic |
| `5*4 и 3*2` | `5*4 и 3*2` | so is one without spaces, and it *pairs* |
| `list_check_off` | `list_check_off` | an identifier is not emphasis |
| `my_file_name.txt` | `my_file_name.txt` | nor is a file name |
| `https://ex.com/a_b_c` | `https://ex.com/a_b_c` | nor a URL |
| `звёздочка*` | `звёздочка*` | R3: a real asterisk in content survives |
| `` (empty) | `` | no crash on empty |

Emphasis markers are stripped only when **all three** hold: they pair on the
same line, no whitespace touches the inside of either marker, and neither
marker sits inside a word — the opening one has no word character before it,
the closing one none after it.

That third condition is not a nicety. Written without it (as this story
originally specified) the converter eats what the bot writes most:
`list_check_off` became `listcheckoff`, `my_file_name.txt` became
`myfilename.txt`, `.../a_b_c` in a URL became `.../abc`, and `5*4 и 3*2`
became `54 и 32`. CommonMark refuses intra-word `_` emphasis for the same
reason; here the rule covers `*` too, since a chat bot writes far more
identifiers and arithmetic than emphasis.

An unpaired `*` is content — R3's third criterion, and the reason this is a
converter rather than a blanket `replace("*", "")`.

**Tests must cover** every row of that table, plus: a multi-line reply mixing
headings, bold and bullets (use the `bugs` file's own "Вот вся информация по
поездке" example — it is the exact reported symptom); text with no Markdown at
all passing through byte-identical; and a string of only whitespace.

- [ ] **Step 1** — write `tests/test_formatting.py` covering the table above.
- [ ] **Step 2** — run it, confirm it fails with `ModuleNotFoundError: No module named 'bot.formatting'`.
- [ ] **Step 3** — implement `bot/formatting.py`.
- [ ] **Step 4** — `uv run pytest tests/test_formatting.py -q`, all green. No database needed.
- [ ] **Step 5** — commit.

---

### Task 2: Apply it to everything the model writes

**Satisfies:** R3

**Files:**
- Modify: `bot/router.py`
- Modify: `tests/test_router.py`

**Interfaces:**
- Consumes: `bot.formatting.to_plain_text` (Task 1).

Apply `to_plain_text` to model-generated text on its way out. Fixed strings
(`_ASK_WHAT_TO_TRACK`, `_GREETING_TEMPLATE`, the `_AI_UNAVAILABLE_MESSAGES`)
contain no Markdown and need no conversion, but running them through it is
harmless and keeps one path instead of two.

**Order matters:** convert **before** the `_has_visible_text` check, not after.
A model that answers with `**<silent>**` must still be silenced, and the
sentinel check compares against bare strings.

**Tests must cover:** a tool-loop reply containing `**bold**` reaching
`send_message` without asterisks; a reply of `**<silent>**` sending nothing;
an ordinary reply passing through unchanged.

- [ ] **Step 1** — write the tests. **Step 2** — watch them fail.
- [ ] **Step 3** — implement. **Step 4** — full suite green. **Step 5** — commit.

---

### Task 3: Answer in Russian

**Satisfies:** R6

**Files:**
- Modify: `bot/router.py` (`_ACTIVE_MODE_SYSTEM_INSTRUCTION`)
- Modify: `tests/test_router.py`

Add to the system instruction, in this spirit and in English (the rest of the
instruction is English; only the bot's *output* is Russian):

> Always reply in Russian, whatever language the incoming message is in. Leave
> proper nouns and list item names exactly as they were written — "Ben Shemen"
> and "sparklers" stay as they are; never transliterate them.

The second sentence is not decoration: R4 makes item names a database key, and
`0001`'s S6 already had a model translate "огурцы" to "cucumbers" and check off
a second copy. Telling it to answer in Russian without that caveat invites
exactly that bug back.

**Tests must cover:** the instruction text names Russian, and it forbids
transliterating names. This is a string assertion, not a model test — worth
having anyway, because the instruction is the whole mechanism and a later edit
could quietly drop it.

- [ ] **Step 1–5** as above.
