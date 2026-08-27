# Story S4: Answering privately

**Part of:** Live-chat feedback (`../EPIC.md`)
**Goal:** "@бот пошли мне в личку список" answers the asker in their DMs.
**Satisfies:** R5
**Depends on:** none
**Parallel-safe with:** none
**Requirements & global constraints:** see `../EPIC.md`

## The one thing that will go wrong

**Telegram refuses to DM anyone who has never started a private chat with the
bot.** This is not an edge case — it is the normal state of most group members,
and `0001`'s S4 already hit it with `nudge_unconfirmed_participants`, where an
unhandled `Forbidden` aborted the whole run. R5's second criterion exists
because of that history: the bot must say, in the group, that it could not
reach them and what to do about it. Silently dropping the answer, or claiming
to have sent it, are both worse than the failure.

---

### Task 1: The tool

**Satisfies:** R5

**Files:**
- Modify: `bot/tools/core.py`, `bot/tools/schema.py`
- Modify: `tests/test_tools_core.py`

**Interfaces:**
- Produces: `send_private_message(pool, telegram_bot, session_id, current_user_id, text) -> dict`

**`current_user_id` is bound by the router, never supplied by the model** —
exactly as `session_id` and `chat_id` already are (`_bind_session_context` in
`bot/router.py`, and `0001`'s S4/S6 where a model-supplied `chat_id` was a
cross-chat write hole). A model that could choose the recipient could DM
anyone in any chat the bot has seen. So the declaration in
`bot/tools/schema.py` exposes **only `text`**, and the signature check that
compares declarations to bound signatures must account for the bound
parameters — read how `_SESSION_BOUND_TOOLS` does it and follow that pattern.

Returns:

| Situation | Return |
|---|---|
| delivered | `{"status": "ok"}` |
| Telegram `Forbidden` | `{"status": "cannot_reach", "detail": "the user has never started a chat with the bot"}` |
| any other send failure | `{"status": "failed", "detail": "<error>"}` |

Never raises: a failed DM must not take down the turn that produced it.

**Tests must cover:** a successful DM going to `chat_id=<the asker's user id>`,
proving it is the asker and not the group; `Forbidden` returning
`cannot_reach` and not raising; another exception returning `failed`; the
declaration exposing only `text`.

- [ ] **Step 1–5.**

---

### Task 2: Wiring and instruction

**Satisfies:** R5

**Files:**
- Modify: `bot/router.py`
- Modify: `tests/test_router.py`

Bind `current_user_id` from `message.from_user.id` when building the registry,
alongside the existing session binding. A message with no `from_user` (channel
posts) must not crash — bind `None` and have the tool return `failed` rather
than attempting a send.

Add to the instruction:

> When someone asks you to send them something privately — "пошли мне в
> личку", "в лс" — call `send_private_message` and reply in the group with one
> short line saying you have sent it. If it returns `cannot_reach`, tell them
> in the group that they need to open a chat with you first and press Start;
> do not repeat the private content in the group.

That last clause matters: the natural model behaviour on a failed DM is to
paste the content into the group, which is precisely what the person was
trying to avoid.

**Tests must cover:** an addressed "пошли в личку" request reaching the tool
with the asker's id bound; the group getting a short acknowledgement rather
than the content; `cannot_reach` producing a group message that mentions
starting a chat and does **not** contain the private text; a message with no
`from_user` not raising.

- [ ] **Step 1–5.**
