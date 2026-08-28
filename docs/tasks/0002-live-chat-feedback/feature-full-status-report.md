# Feature: a fixed-format full status report (event_status)

**Requested:** live, across two rounds of correction with pasted transcripts
**Checked against:** branch `full-status-report`
**Date:** 2026-08-28
**Verdict:** PASS

## What was wrong

Nothing in code ever produced the "как дела с организацией" report as a
whole. The model assembled it itself from `get_facts`/`get_participants`/
`list_show`/`reminder_list`, and it drifted across replies: the emoji
disappeared, "Место" and "Дата" got folded into one line, and "Напоминания"
was missing a section entirely. The instruction added in the previous PR only
covered the shopping list and participant sub-blocks verbatim — it said
nothing about the report's overall shape, because nothing in code defined
one.

## Fix

`bot/status_render.py` (new) defines the exact fixed shape, matching the
user's corrected example byte for byte (modulo trailing whitespace, which is
invisible in Telegram's plain text and not reproduced). `event_status`, a new
composed tool in `bot/tools/composed.py`, assembles it from real data —
`sessions.event_date`, the `place` fact, and the existing `rendered` fields
from `get_participants`/`list_show` plus a new `render_reminders` for the
reminders section — and returns it as one `report` string. The instruction
tells the model to relay `report` character-for-character rather than
composing its own version, the same contract the list and roster renderers
already have.

`session_id` is bound by the router via `_SESSION_BOUND_TOOLS`, not supplied
by the model — the same pattern every other session-bound tool in this
project follows.

## Verified against the exact reported scenario

Reproduced end to end against a real database — chat, session, event date,
a place fact, three participants at three different statuses, a claimed and
an unclaimed shopping item — and asserted the output matches the user's
corrected example line for line:

```
Вот текущая информация по организации встречи:

📍 Место: Tel Aviv, sea
📅 Дата: 20/11

👥 Участники:
◻️ Витька
✅ Андрюха
◻️ Alex

🛒 Список покупок / вещей:

Ещё не разобрали:
◻️ пиво

Уже взяли:
✅ арбуз — Alex

⏰ Напоминания:
Нет запланированных напоминаний
```

The original scenario used "Андрюха" and "Витька" as shopping-list items too
— that reproduction hit the guard from the earlier bugfix PR
(`looks_like_a_participant`), which correctly refused to add them since both
are already participants. Confirms that fix is still doing its job; the test
uses different item names to isolate what this feature actually tests.

## Design decisions made without an explicit spec

- **Place/date block omitted entirely when neither is known** — one more
  application of R7's "nothing invented": a line for a fact nobody has stated
  is worse than no line.
- **The non-empty reminder line format** (message — time, plus repeat and
  target suffixes when relevant) was not given an example by the user; only
  the empty case ("Нет запланированных напоминаний") was specified. Built a
  reasonable, readable format and flagged it here rather than guessing
  silently — worth confirming once a real repeating or targeted reminder is
  visible in a live report.
- **Trailing double-spaces** in the user's pasted template (Markdown-style
  line-break syntax) were not reproduced: Telegram sends plain text with no
  `parse_mode`, so trailing whitespace has no visible effect and several
  Telegram clients strip it anyway.

## Verification method

The renderer is a pure function, tested directly for the exact shape,
place/date omission, and the S1-formatter round-trip guarantee every other
rendered block in this project carries. `event_status` was tested end to end
against a real PostgreSQL, no mocked repositories. The full-shape assertion
was confirmed to fail when the reminders section was removed from the
renderer, both at the unit level and through the composed tool.

Suite: **389 passed**, up from 379.

## Not verified live

Whether the model reliably calls `event_status` for the range of phrasings
that mean "how's it going" and relays `report` verbatim rather than
paraphrasing — the instruction is advisory, like every other prompt-level
rule here, and this is exactly the class of thing that produced both rounds
of the original complaint.
