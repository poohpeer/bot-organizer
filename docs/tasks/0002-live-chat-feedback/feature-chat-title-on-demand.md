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
