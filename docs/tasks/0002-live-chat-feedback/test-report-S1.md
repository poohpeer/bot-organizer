# Test report: S1 — Plain-text Russian replies

**Design:** `docs/tasks/0002-live-chat-feedback/stories/S1-reply-formatting.md`
**Checked against:** branch `0002-S1-reply-formatting`
**Date:** 2026-08-27
**Verdict:** PASS — after fixing 5 defects the story's own behaviour table missed

## Requirement checklist

**R3 — Replies read as written:** PASS
- *"`**Куда:** Ben Shemen` posts without asterisks"* — the exact symptom from
  `bugs`. Covered by `test_formatting.py` and end-to-end by
  `test_router.py`'s bold-reply test, which asserts what reaches `send_message`.
- *"a bulleted list keeps being a list, with `—`"* — covered for `*`, `-` and
  `+` markers, with indentation preserved.
- *"an item whose name genuinely contains an asterisk keeps it"* —
  `звёздочка*` survives, and after the fix so do `5*4`, `list_check_off`,
  `my_file_name.txt` and URLs with underscores.

**R6 — Answer in Russian:** PASS by inspection, which is all this one admits of.
The mechanism is a sentence in the system instruction; the test asserts the
instruction says it and still forbids transliterating names. Whether the model
obeys is not something a unit test can establish — it needs live use, and the
transliteration caveat is there because `0001`'s S6 saw a model turn "огурцы"
into "cucumbers" and check off a second copy.

## Defects found during verification

The story specified a behaviour table; the implementation matched it exactly.
Probing **beyond** the table found five cases, all of which the bot produces
routinely:

| Input | Was | Now |
|---|---|---|
| `вызови list_check_off` | `listcheckoff` | unchanged |
| `snake_case_имя` | `snakecaseимя` | unchanged |
| `my_file_name.txt` | `myfilename.txt` | unchanged |
| `https://ex.com/a_b_c` | `https://ex.com/abc` | unchanged |
| `5*4 и 3*2` | `54 и 32` | unchanged |

Root cause: emphasis required a pair and no touching whitespace, but nothing
stopped a marker **inside a word** from pairing with the next one. The fix adds
a word-boundary condition on both sides. CommonMark refuses intra-word `_`
emphasis for the same reason; this extends it to `*`, since a chat bot writes
far more identifiers and arithmetic than emphasis.

A mangled URL is the worst of these — `web_search` returns real links, and
this silently broke them.

The story's table has been corrected with the reasoning, so the next reader
does not reintroduce it.

## Verification method

`to_plain_text` is pure, so it is tested directly across 21 cases including the
`bugs` file's own "Вот вся информация по поездке" message. The router path is
tested against a real database with the model mocked. The three regression
tests were confirmed to fail with the word-boundary rule removed
(`assert 'вызови listcheckoff' == 'вызови list_check_off'`).

Suite: **275 passed**, up from 250.

## Note for later

Conversion happens on the way out, in one place, before the silence check —
so `**<silent>**` still silences. If a future story sends a message from
somewhere other than the router's reply path (S3's table is a candidate), it
must route through `to_plain_text` too, or asterisks come back for that
message only.
