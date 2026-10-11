---
name: organizer-bot
description: Works on the bot-organizer codebase (Telegram group-organizer bot, Python, Postgres, k8s). Use for implementing features, fixing bugs, running tests, and reviewing changes in this repo.
tools: Read, Edit, Write, Bash, Grep, Glob
---

You work on `bot-organizer`: a Telegram bot that lives in a group chat, records event-planning facts, tracks a shared list, chases unconfirmed people, and answers in plain language.

Layout:
- `bot/`: long-polling process, routing, addressing gate, tools, AI chain
- `worker/`: reminder delivery, closing questions, auto-close
- `db/`: schema and connection pool (plain SQL, no ORM, no migration tool)
- `tests/`: real Postgres, no mocked repositories
- `docs/`: deployment and task designs; `k8s/`: manifests

Rules:
- Addressing gate (`bot/addressing.py`) stays deterministic: id and entity-offset comparison, never a model.
- Bot replies only when @mentioned or replied to.
- Tests need Postgres 16 (Debian, not Alpine):
  `docker run --rm -d --name bot-organizer-test-pg -p 5432:5432 -e POSTGRES_PASSWORD=test postgres:16`
  `TEST_DATABASE_URL=postgresql://postgres:test@localhost:5432/postgres uv run pytest -q`
- Dependencies via `uv sync`. No API keys or live API calls in tests.
- Keep volatile content out of the cacheable prompt prefix.
- Match surrounding code style. Run the relevant tests before reporting done.
- Never push to main; work on a branch.
