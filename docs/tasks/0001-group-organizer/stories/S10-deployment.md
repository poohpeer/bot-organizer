# Story S10: Deployment — Dockerfile, Docker Compose

**Part of:** Group organizer bot (`../EPIC.md`)
**Goal:** Run both processes — the bot (`bot/main.py`, long polling) and
the background worker (`worker/main.py`, poll loop) — plus Postgres, as a
three-service Docker Compose stack with a single `docker compose up -d`,
secrets sourced only from a git-ignored `.env`, and Postgres data
persisted in a named volume that survives container recreation. Mirrors
the sibling `general-telegram-bot` project's proven Compose pattern
(`ghcr.io/astral-sh/uv` base image, `uv sync --frozen --no-dev`).
**Satisfies:** infra only — no new requirement, packages R1–R11's
implementation for deployment.
**Depends on:** S8, S9
**Parallel-safe with:** none (last story)
**Requirements & global constraints:** see `../EPIC.md`

---

### Task 1: Shared image + Compose stack (bot, worker, Postgres)

**Satisfies:** infra

**Files:**
- Create: `Dockerfile`
- Create: `.dockerignore`
- Create: `docker-compose.yml`
- Modify: `.env.example`
- Create: `docs/deployment.md`

**Interfaces:**
- Consumes: `pyproject.toml`/`uv.lock` (S1), `bot/main.py` (S9),
  `worker/main.py` (S8), env vars from the epic's Global Constraints
  (`BOT_TOKEN`, `GEMINI_API_KEY`, `GOOGLE_MAPS_API_KEY`, `DATABASE_URL`,
  `REMINDER_MIN_INTERVAL_HOURS`).
- Produces: a buildable image tagged `bot-organizer`, and a
  `docker-compose.yml` with three services (`postgres`, `bot`, `worker`)
  that `docker compose up -d --build` runs with no manual flags.

- [ ] **Step 1: Write the failing check**
  ```bash
  docker compose config
  ```

- [ ] **Step 2: Run it, confirm it fails**
  Expected: fails — no `docker-compose.yml` in the project root yet
  (`no configuration file provided: not found`).

- [ ] **Step 3: Create `.dockerignore`**
  ```
  .venv/
  venv/
  .idea/
  __pycache__/
  *.pyc
  .git/
  docs/
  tests/
  .env
  .pytest_cache/
  ```

- [ ] **Step 4: Create `Dockerfile`**
  ```dockerfile
  FROM ghcr.io/astral-sh/uv:python3.12-bookworm-slim

  WORKDIR /app
  COPY pyproject.toml uv.lock ./
  RUN uv sync --frozen --no-dev

  COPY bot ./bot
  COPY db ./db
  COPY worker ./worker

  CMD ["uv", "run", "--no-sync", "python", "-m", "bot.main"]
  ```
  The `worker` service overrides `command` to run `worker.main` instead —
  same image, same dependencies, no second Dockerfile.

- [ ] **Step 5: Update `.env.example`**
  ```
  BOT_TOKEN=
  GEMINI_API_KEY=
  GOOGLE_MAPS_API_KEY=
  POSTGRES_USER=bot_organizer
  POSTGRES_PASSWORD=
  DATABASE_URL=postgresql://bot_organizer:<same-password-as-POSTGRES_PASSWORD>@postgres:5432/bot_organizer
  REMINDER_MIN_INTERVAL_HOURS=6
  ```
  (The `DATABASE_URL` host is `postgres` — the Compose service name, not
  `localhost` — Compose puts all services on one internal network where
  they reach each other by service name.)

- [ ] **Step 6: Create `docker-compose.yml`**
  ```yaml
  services:
    postgres:
      image: postgres:16
      environment:
        POSTGRES_DB: bot_organizer
        POSTGRES_USER: ${POSTGRES_USER}
        POSTGRES_PASSWORD: ${POSTGRES_PASSWORD}
      volumes:
        - postgres-data:/var/lib/postgresql/data
      healthcheck:
        test: ["CMD-SHELL", "pg_isready -U ${POSTGRES_USER}"]
        interval: 5s
        timeout: 5s
        retries: 10
      restart: unless-stopped

    bot:
      build: .
      command: ["uv", "run", "--no-sync", "python", "-m", "bot.main"]
      env_file: .env
      depends_on:
        postgres:
          condition: service_healthy
      restart: unless-stopped

    worker:
      build: .
      command: ["uv", "run", "--no-sync", "python", "-m", "worker.main"]
      env_file: .env
      depends_on:
        postgres:
          condition: service_healthy
      restart: unless-stopped

  volumes:
    postgres-data:
  ```
  No `ports:` on any service — the bot and worker only make outbound
  requests (Telegram, Gemini, Google Places, Open-Meteo, Postgres) and
  accept no inbound traffic; Postgres is reachable only from other
  Compose services on the internal network, never published to the host.

- [ ] **Step 7: Create `docs/deployment.md`**
  ```markdown
  # Deployment

  Three services, one `docker compose up`: `postgres`, `bot` (long
  polling), `worker` (reminder + closing-question poll loop).

  ## First run

  1. Copy `.env.example` to `.env` and fill in `BOT_TOKEN`,
     `GEMINI_API_KEY`, `GOOGLE_MAPS_API_KEY`, and a `POSTGRES_PASSWORD`
     of your choosing (put that same password into `DATABASE_URL`).
  2. ```bash
     docker compose up -d --build
     ```
  3. Verify:
     ```bash
     docker compose ps
     docker compose logs -f bot
     docker compose logs -f worker
     ```
     Expect `Starting bot (long polling)` and `Worker started, polling
     every 60s`, with no `KeyError`/connection errors, and both services
     `running` (not `restarting`).

  ## Restarting / upgrading

  ```bash
  docker compose up -d --build
  ```
  Postgres data lives on the `postgres-data` named volume, independent of
  the `bot`/`worker` containers — rebuilding or recreating either service
  never touches it. `docker compose down -v` removes the volume too (only
  use that if you intend to lose all data).

  ## Notes

  - Secrets reach every container only via `env_file: .env` — never
    written into `docker-compose.yml` or the `Dockerfile`. `.env` is
    git-ignored.
  - `restart: unless-stopped` on all three services — if the bot or
    worker crashes (e.g. transient network failure), Docker restarts it
    without manual intervention.
  - Both `bot` and `worker` `depends_on: postgres` with
    `condition: service_healthy` — neither starts until Postgres is
    actually accepting connections, not just "container started".
  ```

- [ ] **Step 8: Build and bring the stack up**
  ```bash
  cp .env.example .env
  # fill in real BOT_TOKEN / GEMINI_API_KEY / GOOGLE_MAPS_API_KEY / POSTGRES_PASSWORD in .env
  docker compose up -d --build
  docker compose ps
  ```
  Expected: all three services (`postgres`, `bot`, `worker`) show state
  `running`, none `restarting`/`exited`.

- [ ] **Step 9: Confirm both app processes started cleanly**
  ```bash
  docker compose logs bot | tail -5
  docker compose logs worker | tail -5
  ```
  Expected: `Starting bot (long polling)` in `bot`'s log,
  `Worker started, polling every 60s` in `worker`'s log — no tracebacks.

- [ ] **Step 10: Confirm Postgres data survives a restart**
  ```bash
  docker compose exec postgres psql -U "$POSTGRES_USER" -d bot_organizer -c "\dt"
  docker compose down
  docker compose up -d
  docker compose exec postgres psql -U "$POSTGRES_USER" -d bot_organizer -c "\dt"
  ```
  Expected: the same set of tables (from `db.pool.init_db`, applied by
  both `bot` and `worker` on startup) is listed before and after —
  `docker compose down` (without `-v`) does not touch the named volume.

- [ ] **Step 11: Commit**
  ```bash
  git add Dockerfile .dockerignore docker-compose.yml .env.example docs/deployment.md
  git commit -m "Add Dockerfile and docker-compose deployment (bot + worker + Postgres)"
  ```
