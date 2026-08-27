# Story S5: Group title and description sync

**Part of:** Live-chat feedback (`../EPIC.md`)
**Goal:** The bot reads the group's own title and description, uses what it
finds, and says when it changes.
**Satisfies:** R7
**Depends on:** none
**Parallel-safe with:** none
**Requirements & global constraints:** see `../EPIC.md`

## Read the epic's Bot API section first

`../EPIC.md` has a section headed *"What the Telegram Bot API cannot do"*. The
short version: **there is no way to list a group's members.** The `bugs` file
asks for every member to be added to the participant list; that is not
buildable, and this story delivers the count plus an incrementally-built roster
instead. Do not spend time looking for a method that does it — verified absent
in `python-telegram-bot` 22.8.

---

### Task 1: Remember what was last seen

**Satisfies:** R7

**Files:**
- Modify: `db/pool.py`
- Create: `bot/group_info.py`
- Create: `tests/test_group_info.py`

```sql
ALTER TABLE chats ADD COLUMN IF NOT EXISTS title_seen TEXT;
ALTER TABLE chats ADD COLUMN IF NOT EXISTS description_seen TEXT;
ALTER TABLE chats ADD COLUMN IF NOT EXISTS info_checked_at TIMESTAMPTZ;
```

**Interfaces:**
- `bot.group_info.fetch(telegram_bot, chat_id) -> dict` —
  `{"title", "description", "member_count"}`. Every field may be None or
  missing: `get_chat` can fail, and a group need not have a description.
  Returns `{}` on any failure and never raises — a polling job must not die
  because one chat is unreachable.
- `bot.group_info.changes_since_last_seen(pool, chat_id, info) -> dict` —
  `{"title_changed": bool, "description_changed": bool, "previous": {...}}`,
  and records the new values plus `info_checked_at`.

**First sighting is not a change.** The first time a chat is seen, both
`_seen` columns are NULL; storing them must report **no** change, or every
chat announces itself the first time the worker polls. R7's third criterion
(silence when nothing changed) covers the steady state; this covers the start.

**Tests must cover:** a first sighting reporting no change and storing the
values; an unchanged second call reporting no change; a changed title
reporting exactly that and not the description; a description going from set
to empty counting as a change; `fetch` returning `{}` when `get_chat` raises.

- [ ] **Step 1–5.**

---

### Task 2: Extract the event from the text

**Satisfies:** R7

**Files:**
- Modify: `bot/group_info.py`
- Modify: `tests/test_group_info.py`

**Interfaces:**
- `async bot.group_info.extract_event(title, description) -> dict` —
  `{"activity_type", "event_date", "place"}`, each None when not stated.

Uses `bot.ai.classify.extract` on the cheap classifier model — this runs on a
timer for every chat, and the epic's constraint is that filter-style checks
never spend the primary model.

**Nothing is invented.** R7's last criterion: a title with no date yields
`event_date: None`, not a guess. The instruction passed to `extract` must say
so explicitly, and `extract` already fails closed to `{}`, which reads as "no
information" here — that is the right default.

Dates must be resolved against the chat's timezone and today's date, the same
way `0001`'s S11 fixed the reminder path: pass the current local date into the
instruction. A description saying "4 июля" with no year means the next 4 July,
not one in the model's training data.

**Tests must cover:** a title with a date and place extracting both; a title
with neither extracting neither; a bare "4 июля" resolving to a future date;
`extract` returning `{}` producing all-None rather than raising.

- [ ] **Step 1–5.**

---

### Task 3: The greeting says what it can see

**Satisfies:** R7

**Files:**
- Modify: `bot/router.py` (`handle_bot_added`)
- Modify: `tests/test_router.py`

On being added, the greeting reports the activity, date and place if the group
info states them, plus the member count from `get_chat_member_count`.

**It still starts no session.** `0001`'s R5 makes explicit consent the only way
a session begins, and being added is not consent. The greeting must therefore
read as "here is what I can see, tag me to start" — describing the trip while
implying nothing is being tracked. It must also not imply it knows who the
members are: it has a number, not a roster.

If the group info yields nothing, the greeting is exactly what it is today —
no empty "Поездка: не указано" scaffolding.

**Tests must cover:** a group with a rich description producing a greeting
naming the activity, date, place and count; a group with a bare title
producing today's plain greeting; no session row created in either case
(R5 regression); the greeting not claiming to know member names.

- [ ] **Step 1–5.**

---

### Task 4: The worker watches for changes

**Satisfies:** R7

**Files:**
- Modify: `worker/main.py`
- Create: `worker/group_sync.py`
- Create: `tests/test_worker_group_sync.py`

**Interfaces:**
- `async worker.group_sync.sync_group_info(pool, telegram_bot) -> dict` —
  `{"announced": [chat_id, ...]}`, added to `poll_once`'s `steps` tuple. It also
  stores `chats.member_count` on each pass — see Task 6.

For every chat with an **active session** — a dormant chat is not being
tracked, and announcing changes there would be the bot speaking unbidden,
which `0001`'s R5 forbids — fetch the info, compare, and on a change post one
message saying what changed and update the session's `event_date` / place fact
accordingly.

`poll_once` already isolates each step in its own try/except and logs only
polls that did something; follow that, and return `{}` when nothing was
announced so an idle poll stays silent in the log.

`GROUP_SYNC_INTERVAL_SECONDS` (default 60) gates how often a given chat is
re-checked, via `chats.info_checked_at` — the worker itself polls every 60
seconds, so without a gate this is one `get_chat` per chat per minute forever.

**One message per change, not one per poll.** The `_seen` columns are updated
in the same call that announces, so a change is announced once. This is R7's
second criterion and the thing most likely to go wrong — a bug here means the
group gets the same announcement every minute.

**Tests must cover:** a changed description producing exactly one message and
the next poll producing none; an unchanged chat producing nothing; a dormant
chat never being fetched at all; one chat's `get_chat` failing not stopping
the others; the announcement naming what actually changed.

- [ ] **Step 1–5.**

---

### Task 5: New members join the roster

**Satisfies:** R7

**Files:**
- Modify: `bot/main.py`
- Modify: `tests/test_main.py`

Telegram delivers joins as `message.new_chat_members`. For a chat with an
active session, add each as a participant with status `unknown` and announce
it — the `bugs` file's "у нас новый участник. Я его добавил в список. Жду
подтверждения."

This is the only roster growth the API allows besides people speaking, which
is why it is worth wiring even though it is small.

Bots joining are ignored — `0001`'s `route_update` already drops messages from
bots, and the same reasoning applies to adding them as participants.

**Tests must cover:** a join in an active-session chat adding a participant and
announcing once; a join in a dormant chat doing nothing; a bot joining being
ignored; a rejoin of someone already listed not duplicating them.

- [ ] **Step 1–5.**

---

### Task 6: Say how much of the roster is missing

**Satisfies:** R7

**Files:**
- Modify: `db/pool.py`, `bot/tools/core.py`, `bot/router.py`
- Modify: `tests/test_tools_core.py`, `tests/test_router.py`

The roster is partial by construction — the API section in `../EPIC.md`
explains why. A list of three names in a group of nine is misleading on its
own, and the person asking cannot tell the difference. So every answer about
participants has to state the gap.

```sql
ALTER TABLE chats ADD COLUMN IF NOT EXISTS member_count INT;
```

Task 4's sync already calls `get_chat_member_count`; store it here on the same
pass. Reading the stored value rather than calling Telegram on every
`get_participants` keeps a chat question off the network path, and a value at
most `GROUP_SYNC_INTERVAL_SECONDS` old is accurate enough for a caveat.

**Interfaces:**
- `get_participants(pool, session_id) -> dict` gains two keys:
  `{"participants": [...], "chat_member_count": int | None, "recorded_count": int}`

`chat_member_count` is None when it has never been fetched — a chat where the
worker has not yet run, or where `get_chat_member_count` failed. R7's last
criterion: the participants are still listed and the remark is simply omitted.
Do not substitute 0, which would read as an empty group.

Add to the instruction:

> When you report on participants and `get_participants` shows
> `chat_member_count` higher than `recorded_count`, add a line: "В чате
> {chat_member_count} человек, но записаны только {recorded_count}." Add it
> only when the numbers differ, and never when `chat_member_count` is null.

**Tests must cover:** `get_participants` returning both counts; a chat with no
stored count returning None for it rather than 0; `recorded_count` matching the
number of participant rows; the instruction carrying the remark's wording and
the condition under which it applies.

- [ ] **Step 1–5.**
