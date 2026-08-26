# Deployment

Three services, one `docker compose up`: `postgres`, `bot` (Telegram long
polling) and `worker` (reminder delivery + closing-question poll loop). The bot
and worker share one image and differ only in their `command`.

## Before the first run: disable privacy mode

**Do this first, or the bot will look completely dead.** Telegram bots have
privacy mode **enabled by default**, and in that mode a plain `@mention` never
reaches the bot at all — only slash commands aimed at it and replies to its own
messages. This bot deliberately has no commands, so with privacy mode on it can
never be triggered.

1. In BotFather: `/setprivacy` → pick the bot → **Disable**.
2. **Remove the bot from the group and add it again.** The setting only takes
   effect on re-join.

To confirm it worked, `getMe` must report `can_read_all_group_messages: true`.

Note that an administrator bot receives every message regardless of this
setting. That is why the bot decides whether to act in code
(`bot/addressing.py`) rather than relying on the platform.

## First run

1. Copy `.env.example` to `.env` and fill it in. Required: `BOT_TOKEN`,
   `GEMINI_API_KEY`, `GOOGLE_MAPS_API_KEY`, and a `POSTGRES_PASSWORD` of your
   choosing — putting that same password into `DATABASE_URL`. Optional:
   `GROQ_API_KEY`, which puts the GPT-OSS models ahead of Gemini in the model
   chain; without it the bot logs a warning at startup and runs on Gemini
   alone. Everything else has a working default. See **Secrets** below.
2. ```bash
   docker compose up -d --build
   ```
3. Verify:
   ```bash
   docker compose ps
   docker compose logs bot
   docker compose logs worker
   ```
   Expect all three services `running` (postgres also `healthy`), and:
   ```
   bot-1     | INFO __main__: Starting bot (long polling)
   bot-1     | INFO __main__: Resolved bot identity: id=… username=…
   bot-1     | INFO telegram.ext.Application: Application started
   worker-1  | INFO __main__: Worker started, polling every 60s
   ```
   The identity line is the useful one: it means the container reached
   Telegram with a valid token. The bot resolves its own username through
   `get_me()` at startup rather than from configuration, so renaming the bot
   cannot silently stop mentions from matching.

## Secrets

There is no secret manager. Every credential lives in exactly one place: the
`.env` file on the deployment host, written by hand.

```bash
cp .env.example .env
chmod 600 .env          # it holds a bot token that grants full control of the bot
$EDITOR .env
```

How that reaches the containers, and where it deliberately does not:

- **Into the containers** via `env_file: .env` in `docker-compose.yml`. The
  values become environment variables of the running process.
- **Never into the image.** Verified on the built image: no `.env` anywhere in
  the filesystem, and no secret values in its config. `.dockerignore` excludes
  `.env`, so a build cannot pick it up even by accident.
- **Never into git.** `.env` is in `.gitignore`; `git check-ignore -v .env`
  confirms which rule covers it.
- **Never into CI.** The workflow defines no secrets and reaches no live API.
  Tests run against fake values from `tests/conftest.py` and a throwaway
  Postgres.

What that buys and what it does not. Anyone with host access — or root, or
Docker socket access — can read `.env`, and `docker inspect` shows a running
container's environment. This is appropriate for a private bot on a machine
you control, and not appropriate for a shared or multi-tenant host. There is no
audit trail and no automatic rotation.

**Rotating a key** is editing `.env` and running `docker compose up -d`. The
containers are recreated with the new environment; Postgres data is untouched.
Revoke the old value at the source too — BotFather's `/revoke` for the bot
token, the provider console for the API keys — since editing the file does not
invalidate anything.

## Running the published image

CI builds and pushes `ghcr.io/poohpeer/bot-organizer` on every merge to `main`.
`docker compose up -d --build` ignores it and builds from the working tree,
which is the simplest thing on a machine that has the source. To run exactly
the artifact that passed CI instead — or on a host with no build toolchain:

```bash
# The package is private because the repository is. A Personal Access Token
# with the read:packages scope is required; a plain `gh auth token` does NOT
# carry it and the pull fails with "denied" even after a successful login.
echo "$GHCR_TOKEN" | docker login ghcr.io -u poohpeer --password-stdin

docker compose -f docker-compose.yml -f docker-compose.ghcr.yml pull
docker compose -f docker-compose.yml -f docker-compose.ghcr.yml up -d
```

Pin a specific build instead of `latest` with `BOT_IMAGE_TAG`, which takes any
tag the workflow pushed — it tags every build with its commit sha:

```bash
BOT_IMAGE_TAG=28c50ea2e53921ae53890fb454c91873891393eb \
  docker compose -f docker-compose.yml -f docker-compose.ghcr.yml up -d
```

## Building the image by hand

```bash
docker build -t bot-organizer .
```

One image serves both processes; `docker-compose.yml` gives each a different
`command`. There is no separate worker image to keep in step.

## Restarting and upgrading

```bash
docker compose up -d --build
```

Postgres data lives on the `postgres-data` named volume, independent of the
containers. Verified: a row written before `docker compose down` is still there
after `up`. `docker compose down -v` **also deletes the volume** — only use it
if you intend to lose all data.

The schema is applied idempotently by both processes on every startup
(`CREATE TABLE IF NOT EXISTS` plus `ALTER TABLE … IF NOT EXISTS`), so upgrading
is just rebuilding. There is no migration tool and no migration step.

## Notes

- **Secrets never enter the image.** They reach the containers only through
  `env_file: .env`. Verified: the built image contains no `.env` and no secret
  values in its config.
- **No published ports.** The bot and worker only make outbound requests
  (Telegram, Gemini, Google Places, Open-Meteo, Postgres), and Postgres is
  reachable only from the other services on the Compose network. Nothing is
  exposed to the host.
- **`restart: unless-stopped` on all three services**, so a crashed process
  comes back without intervention. Note this deliberately does *not* override a
  manual `docker stop` or `docker kill` — those leave the container stopped, by
  design.
- **`depends_on: condition: service_healthy`** — neither app process starts
  until Postgres actually accepts connections, not merely when its container
  starts.

## Timezones

`DEFAULT_TIMEZONE` applies to any chat that has not told the bot where it is.
It must be a name **Postgres** also knows:

```bash
docker compose exec postgres psql -U bot_organizer -d bot_organizer \
  -c "SELECT name FROM pg_timezone_names WHERE name = 'Asia/Jerusalem'"
```

Python's `zoneinfo` accepts about 113 legacy aliases (`US/Hawaii`,
`America/Buenos_Aires`) that Postgres rejects. Per-chat zones set at runtime are
validated against `pg_timezone_names`, but `DEFAULT_TIMEZONE` comes from the
environment and is not — a bad value there breaks the closing-question query.

`QUIET_UNTIL_HOUR` / `QUIET_FROM_HOUR` bound the hours, local to each chat, in
which the bot may start a conversation on its own.

## Logs and troubleshooting

```bash
docker compose logs -f bot worker
```

`LOG_LEVEL` (default `DEBUG`) controls **our** packages only. Third-party
libraries stay at INFO or WARNING whatever it is set to — `httpx` logs a line
per outbound request, `telegram.request` dumps every `getUpdates` payload, and
turning the root logger to DEBUG buries the lines that explain what the bot
decided. See `bot/logging_setup.py`.

At DEBUG you get, for every message:

```
update 42 from chat -100… (supergroup) user=7: @bot что по списку?
update 42: active session 3
session 3: addressed, deciding intent
AI extract -> gemini-3.1-flash-lite | instruction=… | text=…
AI extract <- 210ms | {"reply": "unrelated"}
tool loop -> prompt=… | history=0 turns
tool loop turn 1: model requested list_show
tool -> list_show({'session_id': 3})
DB fetch 1ms -> 3 rows | SELECT name, status FROM list_items WHERE session_id = $1 | args=(3,)
tool <- list_show 2ms | {'items': [...]}
tool loop <- 1840ms after 2 turn(s) | Помидоры — ещё нужно купить…
```

**When the bot doesn't answer**, read from the top of that block: either the
`update …` line is missing entirely (the message never reached the bot — check
privacy mode), or one of the lines right after it says why nothing happened
(`dropped, already seen`, `dropped, sender is a bot`, `not addressed, silent
capture only`).

Set `LOG_LEVEL=INFO` once things are stable; you keep what the bot *did*
(sessions started and closed, items captured, reminders delivered, closing
questions asked) without the per-statement trace.

`LOG_MAX_CHARS` (default 300) caps how much of any single value — a prompt, a
search result, a SQL statement — reaches a log line.

Secrets are never logged: API keys travel in headers that are not rendered, and
a test asserts the maps key cannot appear in `bot.tools.external`'s output.

## Kubernetes

The `k8s/` directory contains the equivalent deployment: one bot pod, one
worker pod, and a single-replica PostgreSQL StatefulSet with a 10Gi PVC. The
bot and worker do not expose HTTP ports; they only make outbound connections.

The GitHub Actions workflow `.github/workflows/deploy-k8s.yml` builds and
publishes the image, then restarts the deployments from a self-hosted runner.
The runner must have `kubectl` installed and a kubeconfig that can access the
target cluster. Application secrets are not stored in GitHub: copy
`k8s/secret.example.yaml` to `k8s/secret.yaml`, fill in the values, and apply
it manually on the Kubernetes machine.

First-time Kubernetes setup:

```bash
kubectl apply -f k8s/namespace.yaml
kubectl create secret docker-registry ghcr-pull-secret \
  --namespace bot-organizer \
  --docker-server=ghcr.io \
  --docker-username=<github-username> \
  --docker-password=<github-pat-with-read-packages>
kubectl apply -f k8s/secret.yaml
kubectl apply -k k8s/
```

The real `k8s/secret.yaml` is gitignored. The GHCR pull secret is also created
manually and is not managed by the workflow.

The workflow currently deploys pushes to `0001-S10-deployment`. Change the
branch filter after merging this work to the branch that should be deployed.
The PostgreSQL PVC is retained when workloads are redeployed; deleting the
PVC or its namespace deletes the stored database.

| Symptom | Cause |
|---|---|
| Bot ignores everything in a group | Privacy mode still on — see above; re-add the bot after disabling |
| `KeyError: 'BOT_TOKEN'` on startup | `.env` missing or incomplete; the app crashes deliberately rather than running half-configured |
| `asyncpg … password authentication failed` | `POSTGRES_PASSWORD` and the password inside `DATABASE_URL` disagree |
| `time zone "…" not recognized` | `DEFAULT_TIMEZONE` is not in `pg_timezone_names` |
| Reminders arrive at the wrong hour | The chat's timezone is unknown and `DEFAULT_TIMEZONE` doesn't match reality; tell the bot where you are and existing reminders are re-anchored |

Every routing decision is recorded in the `decision_log` table with the message
that produced it, which is usually faster than reading logs:

```bash
docker compose exec postgres psql -U bot_organizer -d bot_organizer \
  -c "SELECT created_at, stage, raw_text, decision FROM decision_log ORDER BY created_at DESC LIMIT 20"
```
