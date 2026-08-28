# Fix: the bot could not answer "what's the group's name?" on demand

**Part of:** Live-chat feedback (`EPIC.md`), post-epic live bug
**Satisfies:** R7

## Reported live

> Бот может читать название группы?
>
> [pasted bot reply] "К сожалению, я не вижу названия группы. Подскажите,
> пожалуйста, какое там указано место встречи?"

## Root cause

`chats.title` (the actual stored title column) is written in exactly one
place: `bot.router.handle_dormant_message`, only reached when the bot is
addressed while the chat is still dormant. Once a session goes active,
nothing ever updates it again — a rename after that point is invisible to
the bot forever. `chats.title_seen`/`description_seen` (S5) are kept fresh
by the worker's periodic sync, but only exist for change-detection; nothing
exposed them to the model. There was no tool for the model to read the
chat's title/description on demand at all — the bot's answer above was
literally true, not a bug in wording.

Confirmed by grepping every read/write of `chats.title` and
`chats.title_seen` in `bot/*.py` and `worker/*.py` before writing any code.

## Fix

New composed tool `get_chat_info(pool, telegram_bot, session_id)` in
`bot/tools/composed.py`, calling the existing `bot.group_info.fetch()` live
against Telegram on every call rather than reading a stored column — the
stored `chats.title` is proven stale-after-first-write, and
`chats.title_seen` could still be up to `GROUP_SYNC_INTERVAL_SECONDS` (60s)
behind, or unset entirely on a session just started this second. `fetch()`
already fails closed to `{}` on any Telegram error (kicked, banned, network
blip), so the tool answers `unavailable` rather than a wrong guess.

Declared in `bot/tools/schema.py`, bound in `_SESSION_BOUND_TOOLS`
(`bot/router.py`) so `session_id` is never taken from the model, and the
active-mode system instruction now tells the model to call it whenever
someone asks what the chat is called or what its title/description says,
rather than saying it cannot see it.

## Tests

`tests/test_tools_composed.py`:
- returns the live title/description from a mocked `get_chat`
- a title changed after the session started is still returned correctly,
  and the stored (stale) `chats.title` row is confirmed untouched —
  proof this reads live, not from the stale column
- unknown session → `unknown_session`, no Telegram call made
- `get_chat` raising → `unavailable`, never raises

Regression-tested: sabotaging the function to skip the session lookup and
call `get_chat` with a hardcoded id makes both the wrong-chat-id assertion
and the unknown-session test fail, confirming the tests actually pin this
behavior. Restored and full suite re-run clean.

Full suite: 386 passed, 7 skipped.

## Follow-up: reading was fixed, recording was not

Live check after the above shipped:

> Alex: @pooh_organizer_bot Посмотри в названии группы место встречи и запиши
> Organizer: Записал место встречи: Море.
> Alex: Покажи статус
> Organizer: [status report with no 📍 line at all]
> Alex: Я не вижу место
> Organizer: Место встречи пока не определено.

`get_chat_info` fixed *reading* the title on demand, but a "look at the
title and record it" request needs the model to chain `get_chat_info` and
then `remember_fact` correctly in the same turn — and it didn't: it replied
"Записал" without ever calling `remember_fact`. This is the same class of
bug `event_status` and the deterministic renderers already exist to
prevent (a multi-step action left to the primary model's free composition
instead of guaranteed by code) — just for a write instead of a read.

### Fix

New composed tool `sync_chat_info(pool, telegram_bot, session_id)` in
`bot/tools/composed.py`: fetches the title/description live (same as
`get_chat_info`), runs them through the existing
`bot.group_info.extract_event` classifier (the same one
`worker/group_sync.py` already uses for its own auto-sync), and — in the
same call — persists whatever it finds via `remember_fact("place", ...)`
and/or `UPDATE sessions SET event_date = ...`. Returns what it saved, or
`found_nothing: true` if the title/description state nothing, so the model
reports exactly what happened rather than a guess.

Deliberately a separate tool from `get_chat_info`, not folded into it:
`get_chat_info` is called for any question about the chat's name and must
stay side-effect-free, or an unrelated "как называется чат?" could
silently overwrite a place the group already confirmed by conversation
with stale text from an unchanged title. `sync_chat_info` only runs for an
explicit "record what the title says" request, where overwriting from the
title is exactly the asked-for action.

System instruction (`bot/router.py`) updated to route a "запиши/запомни/
сохрани [то, что в названии]" request to `sync_chat_info` specifically,
and to confirm only using its returned fields — never claim something was
saved without the tool call.

### Tests

`tests/test_tools_composed.py`: saves place and date from a mocked title/
description (verified against both `get_facts` and `sessions.event_date`
directly, not just the tool's return value); nothing found saves nothing;
unknown session; Telegram failure → `unavailable`, never raises.

Regression-tested: sabotaging the function to compute `saved["place"]`
without actually calling `remember_fact` makes the facts-table assertion
fail with `KeyError: 'place'` — reproducing the exact live bug. Restored,
full suite re-run clean: 390 passed, 7 skipped.
