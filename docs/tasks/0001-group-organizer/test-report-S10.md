# Test report: S10 — Deployment (Dockerfile, Docker Compose)

**Design:** `docs/tasks/0001-group-organizer/stories/S10-deployment.md`
**Checked against:** branch `0001-S10-deployment`
**Date:** 2026-08-25
**Verdict:** PASS — verified by running the stack, not by reading the config

Infrastructure only; no new requirement. This report records what was
**observed**, since a compose file that parses proves nothing.

## Checklist

| Claim | Verified how | Result |
|---|---|---|
| One command brings the stack up | `docker compose up -d --build` | all three services `running`, postgres `healthy` |
| The bot reaches Telegram from the container | `get_me()` at startup | `id=8998516801 username=pooh_organizer_bot` |
| The worker starts | container logs | `Worker started, polling every 60s` |
| Schema is created by the app | `\dt` in the Compose Postgres | 10 tables, `proactive_suggestions` correctly absent |
| Secrets never enter the image | filesystem + image config inspection | no `.env` present, 0 secret values in config |
| Postgres data survives restarts | wrote a row, `down`, `up` | row still present |
| Data survives recreating the apps | `up --force-recreate --no-deps bot worker` | row still present |
| `restart: unless-stopped` actually restarts | throwaway container exiting on its own | 5 restarts in 18s; all three services carry the policy |
| Nothing is exposed to the host | no `ports:` in the compose file | Postgres reachable only on the internal network |

## Defects found in the brief

1. **`.env.example` was missing `DEFAULT_TIMEZONE`, `QUIET_UNTIL_HOUR` and
   `QUIET_FROM_HOUR`** — added by S11 after this story was written. Following
   the brief verbatim would have deployed with silent code defaults.
2. **The healthcheck tested the server, not the database.**
   `pg_isready -U ${POSTGRES_USER}` succeeds before `bot_organizer` is
   necessarily ready; `-d bot_organizer` makes `service_healthy` mean what
   `depends_on` relies on it to mean.

## Method correction worth recording

The restart-policy check first used `docker kill`, and the container stayed
down. That is correct Docker behaviour, not a defect: `unless-stopped`
deliberately does not override manual intervention. Two further attempts failed
environmentally (the slim image ships no `ps`/`pkill`; host-side kills are not
permitted), so the policy was proven with a separate container that exits by
itself. The deployment docs now state the distinction explicitly instead of
claiming "Docker restarts it if it crashes" without qualification.

## Not covered here

The stack runs and the bot is connected to Telegram, but **no real group
conversation has been exercised yet** — the bot has not been added to a chat.
Every behavioural requirement (R1–R11) is covered by the 218-test suite and the
per-story reports; what remains untested is the whole thing working in a live
group, which needs a human to add the bot and talk to it.
