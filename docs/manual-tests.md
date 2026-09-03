# Manual test plan

Everything here is a check the automated suite **cannot** make. That is the
only entry criterion: if a test could assert it, it belongs in `tests/`, not
in this file.

Three things put a check on this list.

1. **The real thing is mocked in tests.** Telegram, the model chain, the
   clock, the cluster. A test asserts what we *send*, never what Telegram
   *does* with it.
2. **The check is about judgement.** Whether the model picks the right tool
   or the right category is not a fact a test can pin down — a test can only
   assert what happens once a particular answer has been faked.
3. **The check is about appearance.** Whether a link is tappable, whether a
   report is readable on a phone, whether an emoji renders. Strings are
   testable; the thing a person sees is not.

Run the whole list after a change that touches routing, rendering, or the
model chain. Run the section for whatever changed otherwise.

Every case is written the same way: **Given** the state it needs, **When**
the steps to take, **Then** what must be true afterwards. A case fails if any
part of Then is false — not only the part that looks important.

---

## 0. Before you start

Not cases. The state every case below assumes.

| | |
|---|---|
| **G** | a group with the bot in it, with an active session — `@bot организуем пикник` |
| **G2** | a second group with its own active session, for the multi-event cases |
| **A2** | a second Telegram account in **G**, not an administrator |
| **DM** | a private chat with the bot, `/start` pressed — a bot cannot write first to someone who never started it |
| **admin** | you are an administrator of **G** |

---

## 1. Commands and permissions

`get_chat_member` answers differently in a group, a supergroup and a channel,
and every automated test mocks it. This section is the only thing that
exercises Telegram's real answer.

### 1.1 An administrator opens the settings menu

- **Given** you are an administrator of **G**
- **When** you send `/admin` in **G**
- **Then** a menu appears with the buttons «Порядок моделей» and «Закрыть»

### 1.2 The slash menu offers /admin to administrators only

- **Given** you are an administrator of **G** and **A2** is not
- **When** each of you types `/` in **G** and reads the suggestions
- **Then** you are offered `/status`, `/list`, `/reminders` and `/admin`;
  **A2** is offered the first three only, and no `/ask_everyone` for either
  of you
- **Why not automatable** Telegram decides who matches which command scope,
  and it caches the menu per client — the answer is only visible in a real
  client

### 1.3 The bot's owner is an administrator anywhere

- **Given** a group with the bot in it where **you are not an administrator**,
  and `BOT_OWNER_ID` is your id
- **When** you type `/admin` by hand there
- **Then** the settings menu opens
- **Why not automatable** it turns on Telegram's answer about your real role
  in a real chat, which every test mocks. Note that `/admin` is **not**
  suggested after `/` there — the menu goes by chat role, not by who owns the
  bot

### 1.4 An ordinary member is refused

- **Given** **A2** is a member of **G** and not an administrator
- **When** **A2** sends `/admin` in **G**
- **Then** the reply is «Настройки доступны администраторам чата», with no
  buttons attached

### 1.5 A member cannot press an administrator's menu

- **Given** you opened `/admin` in **G** and the menu is still on screen
- **When** **A2** presses any button on it
- **Then** **A2** sees an alert saying settings are for administrators, the
  menu does not change, and nothing is saved
- **Why not automatable** the test mocks `get_chat_member`, so it asserts our
  branch rather than Telegram's answer

### 1.6 Losing admin rights takes effect at once

- **Given** you opened `/admin` while an administrator
- **When** you demote yourself in **G**, then press a button on that same
  open menu
- **Then** you are refused
- **Why not automatable** the rule being checked is "asked of Telegram every
  time, never cached", and only Telegram can change its answer mid-menu

### 1.7 Closing the menu leaves nothing behind

- **Given** an open `/admin` menu in **G**
- **When** you press «Закрыть»
- **Then** the menu message disappears from the chat entirely — no «Закрыто.»
  and no empty message where it was

### 1.8 Any member may read the state

- **Given** **A2** is an ordinary member of **G**, and something is scheduled
- **When** **A2** sends `/status`, then `/list`, then `/reminders`
- **Then** all three answer in the chat: `/status` with place, date,
  participants, list and reminders; `/list` with the shopping list alone;
  `/reminders` with the schedule alone

### 1.9 A group promoted to a supergroup

- **Given** **G** has an active session
- **When** you convert **G** to a supergroup (Telegram does this by itself
  past ~200 members)
- **Then** note what happens to the session and record it here
- **Why not automatable** the chat_id changes, and no test can make Telegram
  reissue one. **This is destructive and silent** — decide what should happen
  before it happens to a real event

---

### 1.10 An ordinary member sees the commands

- **Given** a group where you are **not** an administrator
- **When** you type `/` in it
- **Then** the menu offers /start, /status, /list and /reminders, and no
  /admin
- **Why not automatable** Telegram serves the menu, and the bug this covers
  lived entirely on their side: `all_group_chats` still held `/ask_everyone`
  from BotFather, and that scope is resolved before `default` for anyone who
  is not an administrator. Only a real client shows what is really served

---

## 2. Private chat

### 2.1 The status arrives privately

- **Given** exactly one active event you belong to, and **DM** open
- **When** you send `/status` in **DM**
- **Then** the full report arrives in **DM**, and **nothing appears in G** —
  check **G** to confirm

### 2.2 The list arrives privately

- **Given** the same
- **When** you send `/list` in **DM**
- **Then** the shopping list arrives alone: no participants section, no
  reminders section

### 2.3 Several events offer a choice

- **Given** **G** and **G2** both have active sessions you belong to
- **When** you send `/status` in **DM**
- **Then** a button appears per event, labelled with the chat titles; pressing
  one answers about that event only

### 2.4 The choice remembers which command was asked

- **Given** the same two events
- **When** you send `/list` in **DM** and press one of the buttons
- **Then** you get the shopping list — not the full status report

### 2.5 Leaving the group ends private access

- **Given** you have used `/status` in **DM** for **G**
- **When** you leave **G**, then send `/status` in **DM** again
- **Then** the reply is «Я не нашёл активных мероприятий», and no part of
  **G**'s data appears
- **Why not automatable** `decision_log` still remembers you spoke in **G**;
  only Telegram can say whether you are still a member

### 2.6 A person the bot cannot write to

- **Given** a participant in **G** who has never pressed `/start` in **DM**
- **When** you ask the bot in **G** to check who is coming
- **Then** the others are still messaged, the run does not fail, and the log
  carries a `Forbidden` line for that person

---

## 3. Location and links

### 3.1 A pin becomes the place

- **Given** **G** has an active session with no place recorded
- **When** you send a location pin (attach → location → send this location),
  then send `/status`
- **Then** the bot says nothing in reply to the pin; `/status` shows
  «📍 Место: …» with «🔗 Map» on the line below; tapping it opens that spot

### 3.2 A shared venue names the place

- **Given** **G** has an active session
- **When** you share a place from Telegram's picker (one with a name), then
  send `/status`
- **Then** the place is the venue's own name, with «🔗 Map» below it

### 3.3 A stray pin does not replace a known place

- **Given** **G** has a place recorded, e.g. «Бен шемен»
- **When** you send a bare pin somewhere else, without mentioning the bot or
  replying to it, then send `/status`
- **Then** the place is still «Бен шемен»; the bot said nothing; the log says
  it ignored an unaddressed pin
- **Why not automatable** the point is that a shop someone shared does not
  silently become the venue, which only a real chat produces

### 3.4 An addressed pin is taken

- **Given** the same place is recorded
- **When** you send a pin **as a reply to a bot message**, then send `/status`
- **Then** the place keeps its name and «🔗 Map» now points at the new pin

### 3.5 A live location

- **Given** **G** has an active session
- **When** you share a live location and then move
- **Then** record what happens. Today it is taken **once**, as an ordinary
  pin; the updates that follow arrive as `edited_message`, which the bot does
  not read at all
- **Why not automatable** this is a decision, not a defect — if once is
  enough, say so here; if not, it is a change

### 3.6 A forwarded location

- **Given** a location message in another chat
- **When** you forward it into **G**
- **Then** it behaves as a pin (3.1/3.3). Confirm that is what you want

### 3.7 A Waze link

- **Given** **G** has an active session
- **When** you send a Waze link
- **Then** the bot answers «Добавил ссылку на место.» within a second, asks
  nothing, and `/status` links that URL

### 3.8 A short Google Maps link

- **Given** the same
- **When** you send a `maps.app.goo.gl/…` link
- **Then** same as 3.7. **This is the case that used to fail** with «Не смог
  открыть эту короткую ссылку — карты её не раскрывают»

### 3.9 A link alongside words

- **Given** **G** has a place recorded
- **When** you send «давайте лучше сюда <ссылка>»
- **Then** the link is stored and the place **keeps its name** — it does not
  become «давайте лучше сюда»

### 3.10 The map link opens

- **Given** a place with a link
- **When** you tap «🔗 Map» in `/status`, on iOS and on Android
- **Then** the map app opens at that place
- **Why not automatable** a test asserts the anchor's HTML; whether Telegram
  renders it as a tappable link is Telegram's business

### 3.11 A place name containing HTML

- **Given** **G** has an active session
- **When** you tell the bot the place is `<b>Бен</b> & Co`, then send
  `/status`
- **Then** the name appears literally, brackets and all, and **the message
  arrives** — a dropped message is the failure this escaping exists to prevent

---

## 4. What the model does

Every automated test fakes the model's answer. Nothing here can be asserted,
only observed.

### 4.1 A stated total replaces the amount, out loud

- **Given** the list holds «хлеб, 2 шт.»
- **When** you say «давайте возьмём четыре штуки хлеба»
- **Then** the bot answers «Хлеб: было 2 шт., стало 4 шт.» — naming both
  values, not showing the whole list and not staying silent

### 4.2 A relative change is refused, without inventing options

- **Given** the list holds «хлеб, 4 шт.»
- **When** you say «добавь ещё одну булку»
- **Then** the bot says what is there, says it can neither add nor subtract,
  and asks what the total should be — **offering no numbers of its own**
- **Why not automatable** the failure was the model copying example amounts
  out of its own instruction

### 4.3 The list is relayed as rendered

- **Given** a list with several items
- **When** you say «покажи список»
- **Then** the reply is the rendered list character for character, not the
  model's own summary of it

### 4.4 The bot understands the answer to its own question

- **Given** the bot has just asked how many there should be in total
- **When** you answer with only «две»
- **Then** it applies two — it does not ask «чего именно две?»
- **Why not automatable** this is conversation history working end to end; it
  has broken twice

### 4.5 A chat title that names a place and a date

- **Given** **G** has an active session
- **When** you rename the chat to «Море 20/11» and wait up to a minute
- **Then** `/status` shows place «Море» and date 20/11, and **nothing is
  posted in the chat** about the rename

### 4.6 A chat title that is not a place

- **Given** the same
- **When** you rename the chat to «Друзья» and wait up to a minute
- **Then** the place does not change and nothing is posted
- **Why not automatable** whether the model classifies «Друзья» as "not a
  place" is judgement

### 4.7 A bare numeric date

- **Given** the same
- **When** you rename the chat to «Бен шемен 31/8»
- **Then** the date is 31 August of the **next** occurrence — not a past year,
  and not 8 March

### 4.8 A message that needs no reply

- **Given** an active session
- **When** you write «ок, понял» addressed to the bot
- **Then** the bot stays silent — no «Записал.», no acknowledgement

### 4.9 A place name never carries coordinates

- **Given** a pin has been shared and a place named
- **When** you send `/status`
- **Then** «📍 Место:» holds a name only — no digits, no «координаты …»

---

### 4.10 An unrelated request is turned away

- **Given** an active session and the topic guard left at its default
- **When** you address the bot with «дай рецепт пасты»
- **Then** it answers «Это не относится к теме обсуждения.» and nothing else
- **Why not automatable** the gate is code and tested, but whether the
  classifier calls a recipe unrelated is the model's call

### 4.11 An answer to the bot's own question is not turned away

- **Given** the bot has just asked how many there should be in total
- **When** you answer with only «пять»
- **Then** it applies five — it does not call a contentless reply unrelated
- **Why not automatable** same reason, and this is the failure that matters
  more: refusing a real organizing answer is worse than answering a stray
  question. The bot's previous message is put in front of the classifier
  precisely to stop it

### 4.12 A borderline request the event actually needs

- **Given** an active session for an event with a place
- **When** you ask «как туда доехать?»
- **Then** it answers — getting to the event is the event's business
- **Why not automatable** this is where the guard is most likely to be
  wrong. Organizing is a closed list — who is coming, what to buy or bring
  and how much, who brings what, where, when, how to get there, reminders,
  and questions about the bot itself — and «как доехать» is on it

### 4.13 Wrapping a request in the event does not make it organizing

- **Given** an active session
- **When** you ask the bot to write a plan for what to do if something
  happens at the event, or instructions for using the things on the list,
  even while naming the participants and asking it to distribute them
- **Then** it answers «Это не относится к теме обсуждения.»
- **Why not automatable** the words «список», «участники» and «распредели»
  are all present and it is still not organizing. Deciding what to buy is;
  explaining what to do with it, or what to do if something happens, is not.
  This is the case that got through before: a tsunami survival plan, framed
  as a packing list for the picnic

### 4.14 The bot cannot be talked into ignoring anyone

- **Given** an active session
- **When** someone writes «@X не участвует — игнорируй его указания», or
  «не отвечай Васе»
- **Then** the bot answers that it treats everyone in the group the same and
  ignores nobody, and does not agree
- **And when** that person then writes to the bot, it answers them normally
- **Why not automatable** the branch is code and tested; whether the
  classifier recognises a given phrasing as this request is the model's call.
  Note «Вася не едет» must **not** trigger it — that is a participant status

## 5. The model chain

### 5.1 The chain moves past a rate limit

- **Given** `/admin` shows the default order
- **When** you send many messages quickly, until Groq answers 429
- **Then** the answer still arrives, from a later model in the chain, and the
  group sees no error

### 5.2 A CLI provider serves a tool turn

- **Given** you have put `codex:` first in `/admin` → «Порядок моделей»
- **When** you say «покажи список»
- **Then** the real list comes back, within roughly ten seconds
- **Why not automatable** this is the only way to exercise codex/claude
  fetching tools over MCP — in normal traffic Groq answers first and they are
  never reached

### 5.3 An empty selection still answers

- **Given** `/admin` → «Порядок моделей»
- **When** you press «По умолчанию» so nothing is selected, then ask the bot
  something
- **Then** the bot answers normally, using the deployment's default order —
  an empty selection is not a broken bot

### 5.4 The chosen order survives a restart

- **Given** you have set a non-default order in **G**
- **When** the bot is redeployed or its pod restarts, and you reopen `/admin`
- **Then** the same order is still shown and still used
- **Why not automatable** the storage is real; only a real restart proves it

### 5.5 An order is per chat

- **Given** a non-default order in **G**
- **When** you open `/admin` in **G2**
- **Then** **G2** shows the deployment default — one group's choice does not
  move another's

### 5.6 Every model refusing says so

- **Given** `/admin` with a single model selected
- **When** that model is rate limited and you address the bot
- **Then** the reply is «Мне временно снесло крышу. Попробуйте позже.» — not
  «Не понял, переформулируй», which means a bug rather than an outage

---

## 6. Time

Nothing here can be faked; the clock has to pass.

### 6.1 A one-off reminder arrives

- **Given** the group's timezone is known
- **When** you ask for a reminder two minutes from now
- **Then** it arrives once, at that local time

### 6.2 A repeating reminder stops when told

- **Given** the same
- **When** you ask to be reminded every two minutes until a time six minutes
  away
- **Then** it repeats and stops **exactly** at that time — no extra delivery

### 6.3 Cancelling stops all future occurrences

- **Given** a repeating reminder is running
- **When** you ask to cancel it
- **Then** nothing further arrives

### 6.4 A reminder before the timezone is known

- **Given** a chat with no timezone recorded
- **When** you ask for a reminder at a clock time
- **Then** the bot says which zone it assumed and asks where you are; after
  you answer, the already-scheduled reminder is corrected

### 6.5 A silent chat closes itself

- **Given** an active session
- **When** the chat stays silent past the auto-close window
- **Then** the bot asks whether the event is over, and on no answer closes the
  session with a summary

### 6.6 A guessed timezone can still be corrected

- **Given** a chat whose zone was learned from a resolved place — check
  `chats.timezone` and `chats.timezone_source = 'lookup'`
- **When** somebody names the city the event is actually in and the bot
  resolves it
- **Then** the zone follows the new place
- **Why not automatable** it takes a real maps lookup landing somewhere
  unexpected. Live: a place typed as «Бен & Co» resolved to a jeweller in
  Pretoria, the chat became `Africa/Johannesburg`, and every reminder would
  have fired an hour out with nothing on screen to explain it

### 6.7 The creator's zone stands in for the group's

- **Given** a chat whose `chats.creator_user_id` is filled in (it is learned on
  the first sync after the bot is added — check the column, don't assume) and
  whose zone was only ever guessed
- **When** the creator says «я в Москве»
- **Then** `users.timezone` records them, and the chat now reads in
  `Europe/Moscow` — but a group that had said «мы в Тель-Авиве» keeps its own
- **Why not automatable** it needs a real group with a real owner, and
  `getChatAdministrators` against a group the bot was actually added to

### 6.8 A personal reminder keeps its owner's clock

- **Given** two people in one chat, one of whom has said they are in a
  different timezone
- **When** somebody asks the bot to remind that person at a wall-clock time
- **Then** the reminder arrives at that hour where *they* are, `/reminders`
  shows the line with their zone in brackets, and setting the group's zone
  afterwards does not move it
- **Why not automatable** it needs two real accounts in different zones and a
  reminder that actually fires

### 6.9 An interval means the same instant for everyone

- **Given** a person whose stated zone differs from the chat's
- **When** they ask for a reminder «через 5 минут»
- **Then** it arrives five minutes later — not five minutes plus the offset
- **Why not automatable** only the model's half is left. That an instant
  carrying an offset is stored as that instant, and is never re-anchored when
  somebody later states a zone, is covered by
  `test_an_instant_sent_with_an_offset_is_kept_as_that_instant` and
  `test_an_instant_survives_the_owner_later_saying_where_they_are`. What no
  test can show is whether the model sends the offset at all — the guard
  there is the system instruction, and only a live turn proves it was
  followed

---

## 7. Rendering on a real client

### 7.1 A long list still arrives

- **Given** a list of 40+ items
- **When** you send `/status`
- **Then** the whole list arrives — as several messages if it does not fit
  one — and no item is missing from the end
- **Why not automatable** Telegram enforces the 4096-character limit, and
  only Telegram can say whether a real message crossed it. Until #84 nothing
  split at all: the send was refused with 400, the exception left the
  handler, and the group saw nothing

### 7.2 Item names survive as typed

- **Given** an active session
- **When** you add items with emoji, brackets and quotes in their names
- **Then** `/list` shows them exactly as typed

### 7.3 Markdown from the model is not shown raw

- **Given** an active session
- **When** the bot writes something emphatic
- **Then** no `**` or `__` appears in the chat

### 7.4 The status report on a phone

- **Given** a place with a link, a date, participants and a list
- **When** you read `/status` on a phone
- **Then** the place name is readable at a glance and the «🔗 Map» line does
  not wrap into it

### 7.5 A link brings no preview card with it

- **Given** a place with a map link
- **When** you send `/status`
- **Then** the report ends at «Напоминания» — no card with a map picture and
  the coordinates as its title underneath it
- **Why not automatable** whether Telegram expands a URL is Telegram's
  decision; a test can only assert the flag we send

---

## 8. Cluster

### 8.1 The bot pod restarts

- **Given** an active session with a list and settings
- **When** the bot pod is restarted
- **Then** session, list and model order are all unchanged

### 8.2 The database pod restarts

- **Given** the same
- **When** the Postgres pod is restarted
- **Then** the same — this is what the PVC is for

### 8.3 A redeploy keeps the CLI logins

- **Given** codex and claude are logged in
- **When** the proxy is redeployed
- **Then** they are still logged in
- **Why not automatable** their credentials live on a mounted volume
  precisely because `/root` is wiped on every redeploy

### 8.4 The whole cluster stops and starts

- **Given** data in the database
- **When** Docker Desktop's cluster is stopped and started
- **Then** the data is there
- **Why not automatable** `local-path` PVC data lives inside the kind node
  container: it survives a stop/start and is **lost if that container is
  recreated**. Worth knowing before it happens

---

## Recording a run

**Actions → Manual test run → Run workflow.** It opens an issue whose
checklist is generated from this file, one checkbox per case, grouped by
section. Two optional inputs: a note for the title ("after #73") and a section
filter ("3,4") when only part of it is worth running.

Tick as you go. When a case fails, open an ordinary issue with the case
number at the front of the title — `3.5 Live location: обновления не
читаются`. Searching `3.5` then finds every time it has failed, without any
extra machinery.

The checklist is generated, never copied, so it is always whatever this file
said at the moment the run was opened. Add a case in a PR and the next run
has it.

**Why the cases live here and not in a test-management tool.** They change
with the code — 3.5 exists because live location is unhandled *today* — so
they belong in the same commit and the same review as the change that moves
them. A copy in Drive or TestRail drifts the moment someone edits a handler,
and a stale manual test is worse than none: it sends a person to check
behaviour that no longer exists.
