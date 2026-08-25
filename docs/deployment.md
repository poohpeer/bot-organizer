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

1. Copy `.env.example` to `.env` and fill in `BOT_TOKEN`, `GEMINI_API_KEY`,
   `GOOGLE_MAPS_API_KEY`, and a `POSTGRES_PASSWORD` of your choosing — putting
   that same password into `DATABASE_URL`. `.env` is git-ignored; keep it that
   way.
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
