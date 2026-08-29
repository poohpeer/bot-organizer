# The MCP endpoint

`bot/mcp_server.py` offers this bot's tools to a model that reaches for
tools itself, rather than asking for them in a response.

## Why it exists

Groq and Gemini take tool declarations in the request and hand back tool
calls in the response, so `bot/ai/tool_loop.py` runs the loop. The Codex and
Claude CLIs cannot work that way: they drive their own loop and call tools
themselves, over MCP. Until this existed, ai-proxy refused their requests
outright (`unsupported_tool_use`), because an adapter that accepts a tools
array and silently ignores it does not degrade — it invents. Asked to show
the shopping list, codex once answered "доступ к данным организатора сейчас
недоступен", which was not true and not something it could know.

Every tool here touches this bot's database, so the server has to live in
this process. ai-proxy only tells the CLI where to find it.

The alternative — relaying tool calls through ai-proxy — was rejected: the
CLIs are one-shot per invocation, so a relay would have to keep CLI sessions
alive between stateless HTTP requests. This keeps ai-proxy stateless and the
loop where the CLI already runs it.

## The grant

`bot/router.py` deliberately never trusts a `session_id` that came from the
model (`_SESSION_BOUND_TOOLS`). In-process that is a correctness measure.
Over a network it becomes an access-control one: a caller that could name its
own session could write into another group's chat.

So a caller presents a token, and **the token decides** which session and
which person the call runs as:

- issued per model turn, unguessable (`secrets.token_urlsafe(32)`);
- short-lived (`MCP_GRANT_TTL_SECONDS`, default 600) and revoked when the
  turn ends, so a leaked token is worth little;
- `session_id` and `current_user_id` are stripped from the advertised schema,
  so there is nothing for a caller to set, **and** stripped again from the
  arguments, so a client that invents the parameter still cannot use it;
- a tool name that is not in the registry is refused, not dispatched.

Grants live in memory. The bot is a single pod with a `Recreate` strategy, so
there is no second process to share with, and a grant that does not survive a
restart cannot be replayed after one.

## Wiring

```
bot (this process)                     ai-proxy                  CLI
  |  issue grant for the turn
  |------------- mcp_url + tools ------->|
  |                                      |--- --mcp-config / -c -->|
  |<---------------- tools/call over HTTP ------------------------- |
  |  runs the tool as the grant says
```

The registry the endpoint dispatches into is `bot.router._build_registry`,
passed in rather than imported, so the binding rules have exactly one
implementation — the one the Groq and Gemini paths already go through.

## The lifetime of a grant

One turn. `bot/router.py` issues a token before calling the model and revokes
it in a `finally`, so a turn that fails does not leave a live token behind —
the TTL is a backstop, not the plan.

The endpoint runs inside the bot process, started in `post_init` and closed
in `post_shutdown`. It is not a second process because it dispatches into the
same registry and the same connection pool this one already holds.

## What the turn did

A grant carries a `TurnRecord` (`bot/turn_outcome.py`): the tools that ran
under it, and the last block a renderer produced. `revoke` hands the grant
back so the router can read it after the turn ends.

It exists because a CLI provider calls its tools *here*, not through
`bot/ai/tool_loop.py`, and the tool loop is where two rules are enforced on
what a turn may say:

- **A rendered block is the answer.** `list_show`, `event_status` and the
  others return text a deterministic renderer produced; a model that answers
  around it gets overruled and the block is sent instead.
- **A turn that changed something has to say so.** `<silent>` is for a
  message that only gives the bot something to record. It is not for a
  message answering a question the bot itself asked.

Without the record the CLI path had neither. Live: the bot asked how many
bottles of beer there should be in total, the person answered "Две бутылки
пива", codex called `list_add`, the row was updated — and codex answered
`<silent>`. The list was correct and the chat saw nothing, which from the
group's side is indistinguishable from the bot being down.

Tools that deliver a message themselves — `send_private_message`,
`broadcast_message`, `send_location`, `nudge_unconfirmed_participants` — do
not count as "changed something and said nothing". Answering in a DM and
staying quiet in the group is the whole point of the first one.

## Configuration

| variable | default | meaning |
|---|---|---|
| `MCP_BASE_URL` | unset | where a CLI reaches this bot. **Unset means MCP is not in use**: no grant is issued and no address is sent, which is the right behaviour for a deployment that does not run it |
| `MCP_PORT` | `8081` | port the endpoint listens on |
| `MCP_GRANT_TTL_SECONDS` | `600` | how long a grant stays valid |

In the cluster `MCP_BASE_URL` is `http://bot-organizer-bot:8081`, a ClusterIP
Service that exists for this and nothing else.

The endpoint is reachable only inside the cluster; it is not exposed through
an Ingress and must not be.

## Checking it by hand

```bash
kubectl exec deployment/bot-organizer-bot -- \
  curl -s -X POST localhost:8081/mcp/<token> \
  -H 'Content-Type: application/json' \
  -d '{"jsonrpc":"2.0","id":1,"method":"tools/list"}'
```

Without a valid token this answers `403` and says nothing else — a caller
with a bad token learns only that it is bad.

## What it buys

`codex:` and `claude:sonnet` sit at the end of `AI_PROXY_MODELS`, behind
Groq and Gemini. They are the reserve, not the front:

| provider | one tool-calling turn |
|---|---|
| groq gpt-oss-20b | 152 in / 32 out |
| claude | 6 in / 150 out, 39 958 cached |
| codex | 5 546 in uncached / 143 out |

Measured on the same task with the same single tool. The reason to keep them
anyway is that they are separate accounts with separate quotas, and this
chain has spent whole evenings against Groq's 429s. codex comes before claude
because it is on a free account — tokens that cost nothing outrank a token
count.

**They only work when `MCP_BASE_URL` is set.** Without it the bot sends no
address, ai-proxy refuses the request with `unsupported_tool_use`, and the
chain moves on — which is the right behaviour, just not a useful one.
