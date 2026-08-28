# bot-organizer

Telegram bot that lives in a group chat and does what "the one organized
person in the group" usually does: listens while an event (a picnic, a trip, a
birthday) is being planned, remembers facts and confirmations, tracks a shared
shopping/todo list, chases people who haven't confirmed, and answers questions
like "what's the plan" or "where do we usually go" in plain conversational
language — no slash commands for the core flow.

It speaks only when spoken to. In an active session it reads every message and
records what matters, but replies only when @mentioned or replied to
(`bot/addressing.py`). That gate is plain id and entity-offset comparison, never
a model, so it cannot drift with a prompt.

## Status

Built and running. All eleven requirements (R1–R11) are implemented, with a
per-story verification report under `docs/tasks/0001-group-organizer/`.

## Layout

| Path | What lives there |
|------|------------------|
| `bot/` | The long-polling process: routing, the addressing gate, tools, AI chain |
| `worker/` | The second process: reminder delivery, closing questions, auto-close |
| `db/` | Schema and connection pool. Plain SQL, no ORM, no migration tool |
| `tests/` | 242 tests. Almost all hit a real Postgres; no mocked repositories |
| `docs/deployment.md` | How to run it, including the BotFather step that is easy to miss |
| `docs/tasks/` | The design, the story briefs, and what verification actually found |

## Running the tests

Every database-touching test needs a real Postgres — the suite has no mocked
repositories, so without one it fails rather than skipping.

```bash
docker run --rm -d --name bot-organizer-test-pg -p 5432:5432 \
  -e POSTGRES_PASSWORD=test postgres:16

uv sync
TEST_DATABASE_URL=postgresql://postgres:test@localhost:5432/postgres uv run pytest -q
```

No API keys are needed and no live API is called: `tests/conftest.py` supplies
fake values. AI requests are sent through the shared `ai-proxy`; configure its
URL with `AI_PROXY_URL` (for Kubernetes this is `http://ai-proxy:8787`).

## Building

The bot and worker share one image and differ only in their `command`.

```bash
docker build -t bot-organizer .
```

CI builds and publishes the same image to `ghcr.io/poohpeer/bot-organizer` on
every push to `main`. See `docs/deployment.md` for running the published image
instead of building locally.

## Running it

See **[`docs/deployment.md`](docs/deployment.md)**. In short: fill in `.env`,
disable privacy mode in BotFather, then

```bash
docker compose up -d --build
```

## Design

The original idea write-up is `docs/idea.md`. The execution-ready design, the
story briefs and the per-story test reports are under
`docs/tasks/0001-group-organizer/` — including what verification found, which
is often more useful than what the design intended.
