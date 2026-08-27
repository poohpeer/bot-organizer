# Epic: Live-chat feedback — what real use asked for

**Goal:** Close the seven feature requests in the repo-root `bugs` file, which
came from running the bot in an actual group chat rather than from the design.

**Architecture:** No new subsystems. Four of the seven are tool and
system-instruction changes inside the existing tool-calling loop; two need a
column or two on existing tables; one adds a third duty to the worker's
existing 60-second poll. The addressing gate, the session state machine and
the model chain are untouched.

**Tech stack:** As `0001-group-organizer` — Python 3.12, `uv`,
`python-telegram-bot` long polling, Groq/Gemini chain, asyncpg on PostgreSQL,
pytest against a real database.

---

## A note on how these task bodies are written

The stories specify **exact interfaces, exact SQL, exact behaviour and the
tests that must exist** — not line-by-line code. This is deliberate, and it is
what worked for `0001`'s S9: SQL and schema in this document have been checked
against the live database, but any Python written here would be unverified, and
in `0001` unverified snippets were transcribed faithfully and shipped their
bugs. Where a decision is subtle, the reasoning is written down so the
implementer can tell an intentional choice from an accident.

---

## Requirements & acceptance criteria

**R1 — Reminders that repeat until told to stop**
> As a group member, I want "напоминай мне каждые полчаса, пока Света не
> ответит" to actually repeat, so I don't have to re-ask every time.

Acceptance criteria:
- **Given** an active session, **when** someone asks for a repeating reminder
  and says how long ("следующие 3 часа", "до завтра до 17:00"), **then** the
  reminder is delivered repeatedly at that interval and stops at that
  boundary, without anyone asking again.
- **Given** someone asks for a repeating reminder **without** saying how long,
  **when** the bot handles it, **then** it asks how long to keep reminding
  instead of guessing, and schedules nothing until answered.
- **Given** a repeating reminder that has passed its stop time, **when** the
  worker next polls, **then** no further copy is delivered and the reminder is
  no longer listed as pending.
- **Given** a repeating reminder, **when** someone cancels it, **then** all
  future repeats stop — cancelling once is enough.
- **Given** a repeat interval shorter than the floor the bot enforces, **when**
  it is requested, **then** the bot says what the shortest interval is rather
  than silently accepting and spamming the chat.

**R2 — See what is scheduled**
> As a group member, I want to ask what reminders are set, so I can tell
> whether the thing I asked for is actually going to happen.

Acceptance criteria:
- **Given** an active session with scheduled reminders, **when** someone asks
  what is scheduled, **then** the bot lists each one with its text, its next
  delivery time **in the chat's own timezone**, whether it repeats, and who it
  goes to.
- **Given** a session with nothing scheduled, **when** asked, **then** the bot
  says there is nothing scheduled — it does not invent entries or claim it has
  no way to check.
- **Given** reminders that were already delivered or cancelled, **when** the
  list is shown, **then** they do not appear in it.

**R3 — Replies read as written**
> As a group member, I want the bot's messages to read as plain Russian text,
> not as raw Markdown, because nothing renders the asterisks.

Acceptance criteria:
- **Given** the model produces `**Куда:** Ben Shemen`, **when** the message is
  posted, **then** the group sees `Куда: Ben Shemen` with no asterisks.
- **Given** the model produces a bulleted list with `*` or `-` at line starts,
  **when** the message is posted, **then** each line begins with `—` and the
  list is still a list.
- **Given** an item whose name genuinely contains an asterisk, **when** it is
  shown, **then** the asterisk survives — only formatting markers are removed.

**R4 — The shopping list as a table**
> As a group member, I want the list to show how much of what, and who is
> bringing it, sorted so I can read it at a glance.

Acceptance criteria:
- **Given** someone says "возьмите картошки" with no amount, **when** the list
  is shown, **then** the quantity column for that item is empty rather than
  invented.
- **Given** someone says "я возьму хлеб", **when** the list is shown, **then**
  their name is against that item.
- **Given** someone who claimed an item says they can't after all, **when** the
  list is shown, **then** the item is unclaimed again and back among the
  unclaimed ones.
- **Given** a list with claimed and unclaimed items, **when** it is shown,
  **then** unclaimed items come first, and within each group items are sorted
  by category and alphabetically inside a category.
- **Given** the list is shown, **then** it is grouped so the unclaimed items
  and the claimed ones are visibly separate, and it stays plain text — no
  Markdown, no HTML, nothing that depends on a font to line up.

**R5 — Answer me privately**
> As a group member, I want to ask in the group and be answered in my DMs, so
> a long list doesn't fill the chat.

Acceptance criteria:
- **Given** someone asks the bot in the group to send something to their DMs,
  **when** the bot answers, **then** the answer arrives as a direct message to
  the person who asked, and the group sees at most a short acknowledgement.
- **Given** the asker has never started a private chat with the bot, **when**
  the bot tries, **then** Telegram refuses and the bot says so in the group,
  telling them to open a chat with it first — it does not silently drop the
  answer or claim to have sent it.
- **Given** someone asks a question the ordinary way, **when** the bot answers,
  **then** the answer goes to the group as before.

**R6 — Answer in Russian**
> As a Russian-speaking group, we want the bot to answer in Russian
> consistently, not to drift into English.

Acceptance criteria:
- **Given** a message in Russian, **when** the bot replies, **then** the reply
  is in Russian.
- **Given** a message in English, or an item with an English name, **when** the
  bot replies, **then** the reply is still in Russian — the group's language
  does not follow one message.
- **Given** a proper noun that has no Russian form ("Ben Shemen", "sparklers"),
  **when** it appears in a reply, **then** it is left as written rather than
  transliterated — R4 already forbids renaming list items.

**R7 — Follow the group's own title and description**
> As a group member, I want the bot to notice when the group name or
> description says where and when we are going, so nobody has to retype it.

Acceptance criteria:
- **Given** the bot is added to a group whose title or description names the
  trip, **when** it greets the chat, **then** it says what it can see — the
  activity, the date and place if stated, and how many people are in the group
  — and still starts no session until asked.
- **Given** an active session and a group description that changes, **when**
  the worker next polls, **then** the bot posts what changed, once per change.
- **Given** the title and description have not changed, **when** the worker
  polls repeatedly, **then** the bot says nothing.
- **Given** a title or description with no date or place in it, **when** it is
  read, **then** nothing is invented and no announcement is made.
- **Given** the bot knows fewer participants than the group has members,
  **when** someone asks how the organizing is going, **then** the answer lists
  who is recorded and adds "В чате X человек, но записаны только Y" — the gap
  is stated rather than left for the reader to notice.
- **Given** every group member is recorded, **when** the same question is
  asked, **then** no such remark appears — it is a caveat, not decoration.
- **Given** the member count cannot be fetched, **when** the question is asked,
  **then** the participants are still listed and the remark is simply omitted.

---

## What the Telegram Bot API cannot do, and what this epic does instead

The `bugs` file asks for "всех участников добавлять в список участников
мероприятия". **The Bot API has no method that lists a group's members.**
Verified against `python-telegram-bot` 22.8: there is `get_chat_member_count`
(a number), `get_chat_administrators` (admins only) and
`get_chat_member(user_id)` (one person, if you already know their id). Nothing
enumerates a roster. This is a platform limit, not an implementation gap, and
no amount of design gets around it.

R7 therefore promises the roster the bot **can** actually build:

| Wanted | Available | This epic |
|---|---|---|
| Every member, on being added | nothing | reports `get_chat_member_count` as a number |
| Names of members | nothing | admins via `get_chat_administrators` |
| New members | `new_chat_members` in updates | added to participants as they join |
| Everyone else | — | added the first time they say something |

Because the roster is partial by construction, every answer about participants
has to say so — see R7's last three criteria. A list of three names in a group
of nine is misleading on its own, and the person asking has no way to know the
difference.

The roster is therefore built up over time rather than known at once. The
greeting must say the count and not imply it knows who everyone is.

---

## Global constraints

- Everything in `0001-group-organizer`'s Global Constraints still binds —
  particularly: the bot acts only when addressed (`bot/addressing.py`), tools
  are declared once in `bot/tools/schema.py` with declarations matching the
  Python signature exactly, `chat_id` is never a model-supplied parameter, and
  the cheap classifier model is used for filter-style checks.
- **Schema changes are plain DDL applied idempotently** — a `CREATE TABLE IF
  NOT EXISTS` block plus `ALTER TABLE ... ADD COLUMN IF NOT EXISTS` in
  `db/pool.py`'s `_ALTERS_SQL`. There is no migration tool and none is being
  added. Every new column must be nullable or have a default: the live database
  has rows in it.
- **Timezone-aware throughout.** Any time shown to a human is rendered in the
  chat's timezone via `bot.timezones.chat_timezone`; anything stored is
  absolute UTC. This is settled — see `0001`'s S11.
- **`REMINDER_MIN_INTERVAL_HOURS` does not apply to repeating reminders.** That
  limit exists so the bot cannot pester one person unbidden; a repeat cadence
  someone explicitly asked for is not unbidden. R1's floor
  (`REMINDER_MIN_REPEAT_MINUTES`, default `5`) is what stops abuse instead.
- **New env vars:** `REMINDER_MIN_REPEAT_MINUTES` (default `5`),
  `GROUP_SYNC_INTERVAL_SECONDS` (default `60`). Both go in `.env.example`,
  `k8s/configmap.yaml` and `docs/deployment.md`.
- Tests run against a real PostgreSQL; no mocked repositories. Telegram and the
  model are the only mocked boundaries.
- All user-facing strings are Russian.

---

## Story index

| ID | Title | File | Satisfies | Depends on | Parallel group |
|----|-------|------|-----------|-----------|----------------|
| S1 | Plain-text Russian replies | stories/S1-reply-formatting.md | R3, R6 | — | — |
| S2 | Repeating reminders and a schedule view | stories/S2-reminders.md | R1, R2 | — | — |
| S3 | Quantity, ownership and order on the list | stories/S3-list-details.md | R4 | — | — |
| S4 | Answering privately | stories/S4-private-replies.md | R5 | — | — |
| S5 | Group title and description sync | stories/S5-group-sync.md | R7 | — | — |

Every requirement R1–R7 appears in exactly one story's `Satisfies` cell.

## Dependency graph

```
S1  (independent)
S2  (independent)
S3  (independent)
S4  (independent)
S5  (independent)
```

No story consumes an interface another produces. They are ordered by value,
not by necessity: S1 improves every message the bot sends and is the cheapest,
so it goes first.

## Parallel groups

**None. Every story runs sequentially.**

Four of the five modify `bot/tools/schema.py` and `bot/router.py`, so they are
not parallel-safe however independent they look — the disjoint-files test
fails. Recording this honestly rather than claiming parallelism the file layout
does not support.

## Execution notes

One branch and one PR per story, cut from `main` after the previous one merges,
per the `implementer` skill's operating rules. Each story ends with the full
suite green (250 passing as of `e0d95b8`) and its own test report under
`docs/tasks/0002-live-chat-feedback/`.
