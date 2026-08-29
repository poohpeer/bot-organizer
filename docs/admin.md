# `/admin`

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

```
Порядок моделей. Бот идёт по списку сверху вниз и переходит к следующей,
когда предыдущая недоступна.

1. openai/gpt-oss-120b        [▲] [▼] [✅]
2. openai/gpt-oss-20b         [▲] [▼] [✅]
...
— codex:          (выключена)  [▲] [▼] [◻️]
```

Every model the deployment offers is listed, enabled or not, so turning one
back on is possible from the same screen that turned it off — a menu that
hides what you disabled cannot undo itself. Disabled ones sort below the
enabled ones and show no position.

**Сбросить** deletes the chat's row rather than storing today's default, so
the chat follows the deployment again if the deployment changes.

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
