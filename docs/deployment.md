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

1. Copy `.env.example` to `.env` and fill it in. Required:
   `BOT_ORGANIZER_BOT_TOKEN`, `GOOGLE_MAPS_API_KEY`, and a `POSTGRES_PASSWORD`
   of your choosing — putting that same password into `DATABASE_URL`.
   Everything else has a working default. See **Secrets** below.

   **No model API keys.** This bot calls no model provider directly: every
   model call goes to the `ai-proxy` service named by `AI_PROXY_URL`, and
   ai-proxy is the only component holding provider credentials. If model
   calls fail, look at ai-proxy's own configuration and logs, not at this
   bot's secrets. `GOOGLE_MAPS_API_KEY` is not an exception to this — it is
   for place lookup, not a model.
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

The bot uses the shared proxy service. For a Compose deployment set
`AI_PROXY_URL=http://ai-proxy:8787` (or the reachable service URL) in `.env`.
For Kubernetes this is already configured as `http://ai-proxy:8787` in the
ConfigMap; the proxy and bot must be in the same namespace.
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

## `BOT_OWNER_ID`

One Telegram user id. That person counts as an administrator for `/admin` in
every chat the bot was added to, whether or not they run that chat — the
models it calls are spent from their account, so the provider chain is a
decision about their money.

Checked before Telegram, so it cannot be lost to a failed `get_chat_member`.
Leave it unset in a deployment nobody owns personally: then only a chat's own
administrators qualify. A malformed value is treated as unset.

## Timezones

`chats.timezone` is set two ways, and `chats.timezone_source` records which:

- **`stated`** — a human said where they are, and `set_timezone` recorded it.
  Nothing overrides this: someone saying "мы по Москве" knows better than the
  coordinates of a restaurant they looked up.
- **`lookup`** — guessed from a resolved place's coordinates. A later lookup
  replaces an earlier one, because the newer one was made with more of the
  conversation behind it.

A row with no source predates the column and counts as a guess — that is what
most of them were, and the alternative is a chat stuck on one forever.

The distinction exists because "never overwrite" protected a bad guess as
firmly as a human's word. Live: a place typed as «Бен & Co» resolved to a
jeweller in Pretoria, the chat became `Africa/Johannesburg`, and the group
saying "мы в Тель-Авиве" afterwards moved the place but not the zone —
leaving every reminder an hour out.

### Whose clock a time is read in

Three things can decide it, strongest first:

1. **What the group said about itself** — `chats.timezone` with source
   `stated`. A group that has said where *it* is has said something the
   creator's own whereabouts cannot override; the creator may be travelling.
2. **The group creator's own zone** — `users.timezone` for
   `chats.creator_user_id`. Learned from `getChatAdministrators` the first
   time a chat is synced (or the moment the bot is added), and looked up only
   while `creator_user_id` is NULL: an owner change is rare enough not to be
   worth an API call on every sync.
3. **The guess** — `chats.timezone` with source `lookup`.

A person's own zone (`users.timezone`) is only ever set by them saying so —
`set_timezone` with `whose='me'` ("я в Москве", as against "мы в Москве").
There is no coordinate lookup for a person: a location someone shares is
almost always the venue, not where they are standing.

### Personal reminders

A reminder with a `target_user_id` whose owner has stated a zone is read in
*their* wall clock: «напомни Васе в 9» means nine o'clock where Вася is,
whatever time it is in the group. `/reminders` renders each line on the clock
it will actually keep and names the zone when it is not the chat's.

Consequences worth knowing:

- Setting the **group's** zone does not move personal reminders — the group
  learning where it is says nothing about where Вася is.
- Someone stating their **own** zone moves everything addressed to them, in
  every chat, and — if they created the chat and their zone is what it falls
  back to — that chat's group reminders too.
- An **interval** ("через 20 минут") means the same instant for everyone. The
  system instruction tells the model to send those with an explicit offset,
  which `to_utc` passes through untouched; a bare local time would be re-read
  in the target's zone.

## One event per group

A chat has at most one active session, and that is now said out loud rather
than enforced by accident. Somebody proposing a second event — «а поехали на
море» while a picnic is being tracked — is told the bot can only follow one
and asked whether to switch. Nothing changes until they say yes.

A confirmed switch **retopics** the session rather than closing and reopening
it: the shopping list, the participants and the pending reminders are what
the group built by hand, and most of them survive a change of plan. The date
is only overwritten when a new one was actually stated — switching from a
picnic to a trip says nothing about when the trip is.

## What starts a session

Only what somebody wrote. The group's own title used to be handed to the
classifier as context, and it decided the outcome: a chat called «Море 3/9»
saying «начинай это отслеживать» was refused, because no word anywhere named
an *activity* — while the same message in «Пикник на море 3/9» started a
session immediately. The title is no longer part of that decision.

The greeting the bot posts when it joins still reads it back — «Вижу:
мероприятие, дата — 03.09.2026, место — Море. В группе 11 человек.» —
because reporting is not deciding. It was removed along with the decision at
first, which was wrong: saying what can be seen, before anything is tracked,
is the whole point of the greeting.

An unnamed activity is not a refusal any more. A session with nothing to call
it is stored as «мероприятие»; the code already did that, and only the
classifier's confidence flag stood in the way.

This is separate from the sync below, which still applies a title change to
an event **already** being tracked.

## Group title/description sync

`GROUP_SYNC_INTERVAL_SECONDS` (default `60`) caps how often the worker calls
`get_chat` for a chat with an active session, to notice a changed title or
description. The worker's own poll runs every 60s regardless; this is a
separate, per-chat gate on top of that, not a second poll loop.

**Nothing is posted to the chat.** A group renaming its own chat can see that
it did. What the sync does is bring the event into line with the new text:

- **A date is taken from the title or description** whenever one is stated,
  a bare `20/11` included — group titles are commonly a place and a date
  together ("Море 20/11", "Маленькая прага 31/8").
- **A place is taken only when the text names somewhere the group could
  actually go.** The classifier answers a `place_kind` out of a fixed set —
  `город`, `адрес`, `заведение`, `природа`, `нет` — and the code stores a
  place only on a positive, known answer. A chat called "Друзья" or
  "Отдыхаем" has no place in it, and an unrecognised answer counts as `нет`.
  It has to be the model's judgement: resolving the text through maps would
  look like enforcement and is not, because maps returns a result for almost
  any string.
- **A value is written only when it differs** from what the session already
  holds. `facts` rows are append-only and `get_facts` takes the newest per
  key, so re-recording an unchanged place on every touch of the title would
  grow a pile of identical rows.

### Why a change is recorded last, not first

`changed_fields` compares; `record_as_seen` stores; the worker calls the
second only after the first has been acted on. They used to be one call, so a
change was consumed the instant it was noticed.

Live: the title moved to "Маленькая прага 31/8", the old code marked it seen,
the classifier chain then answered 400, and the change was gone — the next
pass saw no difference and the event never learnt the new place. The chat got
an announcement about a change that had been applied to nothing.

A classifier that could not answer at all (`event["answered"] is False`)
leaves the change unrecorded so the next pass retries. A classifier that
answered and found nothing does record it — otherwise a chat called "Друзья"
is re-read, and a model call spent on it, every minute forever.

`bot.tools.composed.sync_chat_info` — the same job on demand, when someone
asks the bot to read the group's title — goes through the same
`group_info.apply_event`, so all of the above holds there too.

## A location shared in the chat

Someone dropping a pin (or sharing a place from Telegram's picker) sets the
event's place, and the status report then carries a map link on its own line
under the name:

```
📍 Место: Маленькая прага
🔗 Map
```

`🔗 Map` is the link. A word rather than the URL: coordinates are unreadable
and a raw maps link is three lines long on a phone. Beside the name the two
ran together and the name stopped being readable at a glance, which is the
one job that line has.

**A place name never carries coordinates.** Live, the model recorded the
place as `Бен шемен, координаты 31.9460200, 34.9434050` — the same mistake as
an item called "2 кг мяса", and it costs the same two things: the line reads
like a database row, *and* the name matches nothing in `places`, so the
coordinates that would have made a link are unreachable. `remember_fact`
strips a trailing coordinate pair from the place key, where the value is
written rather than where it is read — several callers write it and only one
of them is the model.

### A navigator link is taken as given

A Waze, Google Maps, Yandex, Apple Maps or similar link in the chat sets the
event's place link — **not resolved, not expanded, not checked**. Live, the
model tried to look one up and answered:

> Не смог открыть эту короткую ссылку — карты её не раскрывают. Пришли,
> пожалуйста, название места или точку, которая открывается…

The link opens perfectly well on the phone of whoever receives it. Whether
maps can expand a short link says nothing about whether it works, so the
lookup — and the question after it — turned a working link into a
conversation.

Handled in `bot/router.py::handle_shared_map_link`, before the model, because
there is nothing to decide. Recognised by host, plus any URL carrying a
coordinate pair — that is the catch-all for a navigator nobody listed.

It is **additive**: only the link is set. The place keeps whatever the group
called it; a link is not a rename. A name is taken only when there is none at
all *and* the message said something besides the URL.

Stored in `sessions.place_url`, kept byte-for-byte. Not in `places`: that
table's `lat`/`lon` are `NOT NULL` and a short link carries neither —
expanding one to find out is exactly what this exists to stop. A link
somebody sent wins over one built from coordinates, since a short link often
points at a pin no lookup would have found.

### The link needs entities

`parse_mode` is set nowhere else in `bot/` on purpose: Telegram drops a whole
message on unbalanced entities, and names come from users. So renderers write
a link as a **marker** (`bot/telegram_text.py`), and that module is the only
thing that turns one into HTML. Two properties keep it safe:

- a message with no marker is sent exactly as before — plain, no `parse_mode`
  at all — so nothing that works today can start failing;
- a message with one is escaped in full first, and the only tags that survive
  are the anchors the module writes itself, so entities are balanced by
  construction. A place called `<b>Бен</b> & Co` is shown, not interpreted.

A message carrying a link also goes out with its preview disabled. Telegram
expands the first URL it finds into a card, and for a maps link that card is
a picture of the map with the coordinates as its title — half a phone screen
of it, pushed under a report whose whole point is being read at a glance. The
link is already a link.

A message too long for one send goes as several, cut at line boundaries.
Telegram refuses anything past 4096 characters with 400 "message is too
long", and nothing caught that: the exception left the handler and the group
saw nothing at all.

Lines are the seam because everything long here is a list, and a cut mid-line
splits an item in half — or, with HTML, an entity, which loses the whole
message rather than making it ugly. Every anchor lives inside one line, so a
line boundary is always safe. A single line longer than the limit is cut by
characters; that needs somebody to have typed a 4000-character item name, and
the alternative is sending nothing.

Truncation is not on offer: a list exists to be complete, and a report that
silently stops halfway is worse than two messages.

`send_text` is used at the two places a report can leave the process: the
`/status` and `/list` commands, and the model relaying `event_status`
verbatim. `strip_links` reduces a marker to its label for everywhere else —
`decision_log`, which people read and `bot/history.py` feeds back to the
model.

Only `http://` and `https://` become links; anything else stays as the plain
label.

**Not every pin is the venue.** People share shops, stations, where they are
standing. Silently replacing a place the group agreed on by conversation is
the same kind of destruction as overwriting an amount nobody asked to change,
so a pin is only taken when:

| shared | place already recorded | taken? |
|---|---|---|
| venue (named) | — | yes |
| bare pin | no | yes, named after its own coordinates |
| bare pin, addressed to the bot | yes | yes — coordinates only, the name survives |
| bare pin, not addressed | yes | **no**, logged and ignored |

A bare pin with no place yet is stored under its coordinates as the name, so
the report shows a working link immediately; the first person to name the
place replaces the label without losing the pin.

Pinning the same place twice is a **correction**, not a duplicate: the
coordinates move and `resolved_at` is refreshed. A bare pin never blanks out
an address a venue or a maps lookup already supplied.

The coordinates the report links are looked up for **the name being
reported**, not the most recently resolved row — otherwise a pin someone
dropped for a supermarket would appear beside the picnic's own place.

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
resources use the current Kubernetes namespace and have `bot-organizer-`
prefixed names so multiple bots can share one namespace without collisions.

The GitHub Actions workflow `.github/workflows/ci.yml` tests, builds and
publishes the image, then restarts the deployments from a self-hosted runner.
The runner must have `kubectl` installed and a kubeconfig that can access the
target cluster. Application secrets are not stored in GitHub: copy
`k8s/secret.example.yaml` to `k8s/secret.yaml`, fill in the values, and apply
it manually on the Kubernetes machine.

First-time Kubernetes setup:

```bash
kubectl create secret docker-registry bot-organizer-ghcr-pull-secret \
  --docker-server=ghcr.io \
  --docker-username=<github-username> \
  --docker-password=<github-pat-with-read-packages>
kubectl apply -f k8s/secret.yaml
kubectl apply -k k8s/
```

The real `k8s/secret.yaml` is gitignored. The GHCR pull secret is also created
manually and is not managed by the workflow.

The workflow deploys pushes to `main`, and only after the test suite passes.
The PostgreSQL PVC is retained when workloads are redeployed; deleting the
PVC deletes the stored database.

| Symptom | Cause |
|---|---|
| Bot ignores everything in a group | Privacy mode still on — see above; re-add the bot after disabling |
| `KeyError: 'BOT_ORGANIZER_BOT_TOKEN'` on startup | `.env` missing or incomplete; the app crashes deliberately rather than running half-configured |
| `asyncpg … password authentication failed` | `POSTGRES_PASSWORD` and the password inside `DATABASE_URL` disagree |
| `time zone "…" not recognized` | `DEFAULT_TIMEZONE` is not in `pg_timezone_names` |
| Reminders arrive at the wrong hour | The chat's timezone is unknown and `DEFAULT_TIMEZONE` doesn't match reality; tell the bot where you are and existing reminders are re-anchored |

Every routing decision is recorded in the `decision_log` table with the message
that produced it, which is usually faster than reading logs:

```bash
docker compose exec postgres psql -U bot_organizer -d bot_organizer \
  -c "SELECT created_at, stage, raw_text, decision FROM decision_log ORDER BY created_at DESC LIMIT 20"
```
