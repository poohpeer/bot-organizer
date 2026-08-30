# Commands

## `/status` and `/list`

`/status` posts the full organizing report — place, date, participants,
shopping list, reminders. `/list` posts just the shopping list. Any member
may use either.

They are the same fixed blocks `bot.tools.composed.event_status` and
`bot.tools.core.list_show` build, so a command and the model's answer can
never drift apart. They spend **no model call**: asking "как дела с
организацией" costs a turn of the primary chain and depends on the model
relaying the report verbatim — `bot/turn_outcome.py` exists to force that,
which is itself evidence it does not always happen.

No admin check, unlike `/admin` below: these only read, and every member can
already see all of it in the chat.

### In a private chat

Both work in a DM, so someone can check the list without posting anything to
the group — the whole reason for asking privately.

**Read-only, deliberately.** Every change made in the group is visible to
everyone there, and that visibility is most of what makes a shared list
trustworthy: "добавь пива" from a private chat would move the group's list
with nobody able to see who did it or when. Looking disturbs no one, so
looking is what a DM allows.

A private chat has no session of its own, so `bot/dm.py` decides which event
the question is about:

| this person's active events | what happens |
|---|---|
| exactly one | answered straight away |
| several | a button per event; the choice carries which command was asked |
| none | told so |

**Membership is the gate, asked of Telegram every time.** Candidate events
come from `participants` and `decision_log` — the two places a user id is
recorded against a chat, since the Bot API cannot list a group's members. But
being in `decision_log` is not permission; being in the chat *now* is. Any
failure to check answers no: someone who has left must stop seeing the list
immediately, and a private read is the wrong place to fail open.

The session id in a button's callback data is re-checked on press for the
same reason — the keyboard stays live in the private chat, and membership can
end after it was drawn.

## `/admin`

The configuration menu. One setting so far: which models this chat tries,
and in what order.

## Who may use it

Chat administrators, checked against Telegram every time — including on each
button press, not only when the menu is opened. A keyboard stays live in the
chat afterwards, so anyone can press it, and whoever opened it may since have
been demoted.

If the check itself fails — Telegram unreachable, an unexpected reply — the
answer is no. A settings menu is the wrong place to fail open.

## The chain

Press the models in the order the bot should try them. The first press takes
number one, the second number two. Pressing a model already in the chain
takes it out, and everything after it moves up.

```
Порядок моделей. Нажимайте их в том порядке, в каком бот должен их
звать: первое нажатие — первый номер. Нажать ещё раз — убрать.

┌────────────────────────────────┐
│ 1️⃣  codex:                     │
│ 2️⃣  claude:sonnet              │
│ ◻️  openai/gpt-oss-120b        │
│ ◻️  gemini-3.7-flash           │
│ …                              │
├────────────────────────────────┤
│  По умолчанию  │     Назад     │
└────────────────────────────────┘
```

One button per model, full width: the names run to `openai/gpt-oss-120b` and
the three-button rows this replaced left no room to read them.

It replaced a ▲ ▼ ✅ pair per row, where every position was one press and one
round trip — lifting the last model to the front took seven. Now any order
costs one press per model you actually want, and models you do not want you
simply never press.

Every model the deployment offers stays listed, chosen or not: a menu that
hides what you did not pick cannot undo itself. Chosen ones sort to the top
so the chain reads down the screen.

**Закрыть** deletes the menu message. It used to write "Закрыто." over
itself — a message about the bot's own furniture, answering a question nobody
asked and staying in the chat forever, next to the plans people came to read.
A bot may only delete its own message for 48 hours; an older menu loses its
keyboard instead, so it cannot be pressed, and still nothing new is written.

**По умолчанию** deletes the chat's row. That is both "clear the selection"
and "back to the deployment default" — with the fallback below they are the
same state, so the menu offers one button rather than two that look different
and are not.

It used to say **Очистить**, and only one of the two readings is ever what
anyone wants. Pressed in the sense of "done", it threw away the order just
built — which looks exactly like the setting failing to survive a restart.
It does survive: the chain lives in `chat_settings`, and one live row was
still there 17 hours and eight pod restarts after it was saved.

### Choosing nothing is allowed

An empty selection is not a broken bot: the chat falls back to
`AI_PROXY_MODELS`, and the menu says which chain that is instead of warning.
It is also how you start over — under the old menu, starting over meant
disabling eight models one at a time.

This is why `bot/settings.py` has both `get_stored_chain` and
`get_provider_chain`. The second answers "what will be tried" and substitutes
the default, so it cannot tell a chat that picked nothing from one that
picked the whole default; the menu needs that difference, because one draws
eight numbered models and the other draws none.

## Scope

Per chat. The chain is a routing decision, and one group changing it for
every other group is not a setting, it is a surprise. A chat that has never
opened this menu uses `AI_PROXY_MODELS` unchanged.

`AI_PROXY_MODELS` stays the source of what exists: a stored choice naming a
model the deployment no longer offers has that entry dropped, and a stored
chain with nothing usable left falls back to the default entirely. Better a
working default than a chain ai-proxy will refuse.

## What changing it does

Each chat gets its own `ModelFallback`. That object is sticky on purpose —
having learned a model is rate limited, it stays past it rather than spending
another call to be told again — and before this it was a single shared
instance, so one busy chat pushed every other chat down the chain with it.

Changing the order rebuilds that chat's chain, which resets the stickiness.
That is the right moment for it: the reason to stay past a model does not
apply to a chain it may no longer be in.

## Storage

`chat_settings (chat_id, key, value JSONB, updated_at, updated_by)`. Keyed by
`(chat_id, key)`, so the next setting needs no migration — only a new key.
