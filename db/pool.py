import json
import logging
import time

import asyncpg

from bot.logging_setup import truncate

log = logging.getLogger(__name__)


# asyncpg runs these itself on every acquire/release, and our own setup hook
# re-pins the schema just as often. Logging them puts two lines of pool
# housekeeping around every single real query, which is how a debug log stops
# being readable.
_HOUSEKEEPING = ("SELECT pg_advisory_unlock_all()", "SET search_path", "RESET ALL")


def _is_housekeeping(query: str) -> bool:
    stripped = query.strip()
    return any(stripped.startswith(prefix) for prefix in _HOUSEKEEPING)


def _sql_summary(query: str) -> str:
    """The shape of a statement, not the statement.

    Our SQL is heavily commented and formatted across many lines; logging it
    verbatim on every call would make the log unreadable and bury everything
    else. Comments are cut to end-of-line — dropping only the `--` token would
    leave the entire explanation behind it in the log, which is most of the
    text in several of these statements.
    """
    lines = [line.split("--", 1)[0] for line in query.splitlines()]
    return truncate(" ".join(" ".join(lines).split()), 110)


class _LoggingConnection(asyncpg.Connection):
    """Logs every statement with its duration and result size at DEBUG.

    Done at the connection class rather than at each call site: there are
    dozens of queries across bot/ and worker/, and instrumenting them
    individually would guarantee the interesting one is the one nobody
    instrumented. Parameters are logged too — this bot stores group-chat
    logistics, not credentials — but truncated, and rows are counted rather
    than dumped.
    """

    async def _log_call(self, kind, query, args, run):
        if _is_housekeeping(query):
            return await run()
        started = time.perf_counter()
        try:
            result = await run()
        except Exception as e:
            log.warning(
                "DB %s FAILED in %.0fms: %s | args=%s | %s",
                kind, (time.perf_counter() - started) * 1000,
                _sql_summary(query), truncate(args, 120), e,
            )
            raise
        elapsed = (time.perf_counter() - started) * 1000
        if log.isEnabledFor(logging.DEBUG):
            if isinstance(result, list):
                size = f"{len(result)} rows"
            elif result is None:
                size = "no row"
            elif kind == "execute":
                size = truncate(result, 40)
            else:
                size = "1 row"
            log.debug(
                "DB %s %.0fms -> %s | %s | args=%s",
                kind, elapsed, size, _sql_summary(query), truncate(args, 120),
            )
        return result

    async def fetch(self, query, *args, **kwargs):
        return await self._log_call(
            "fetch", query, args, lambda: super(_LoggingConnection, self).fetch(query, *args, **kwargs)
        )

    async def fetchrow(self, query, *args, **kwargs):
        return await self._log_call(
            "fetchrow", query, args, lambda: super(_LoggingConnection, self).fetchrow(query, *args, **kwargs)
        )

    async def fetchval(self, query, *args, **kwargs):
        return await self._log_call(
            "fetchval", query, args, lambda: super(_LoggingConnection, self).fetchval(query, *args, **kwargs)
        )

    async def execute(self, query, *args, **kwargs):
        return await self._log_call(
            "execute", query, args, lambda: super(_LoggingConnection, self).execute(query, *args, **kwargs)
        )

_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS chats (
    chat_id    BIGINT PRIMARY KEY,
    title      TEXT,
    -- IANA name ("Europe/Moscow"), or NULL to use DEFAULT_TIMEZONE. Telegram
    -- never tells us this, so it is learned from a resolved place or stated by
    -- a human; see bot/timezones.py.
    timezone   TEXT,
    -- Whoever created the group, from get_chat_administrators. Their own
    -- stated zone stands in for the chat's when nobody has stated the
    -- chat's -- see bot/timezones.py.
    creator_user_id BIGINT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- What a person told us about themselves, independent of any one chat.
-- Telegram exposes no timezone for a user any more than it does for a chat,
-- so the only row here is one somebody actually stated out loud.
CREATE TABLE IF NOT EXISTS users (
    user_id    BIGINT PRIMARY KEY,
    -- IANA name, or NULL when this person has never said where they are.
    timezone   TEXT,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS sessions (
    id                          BIGSERIAL PRIMARY KEY,
    chat_id                     BIGINT NOT NULL REFERENCES chats(chat_id),
    activity_type               TEXT NOT NULL,
    status                      TEXT NOT NULL DEFAULT 'active'
                                     CHECK (status IN ('active', 'closed')),
    event_date                  DATE,
    event_date_raw              TEXT,
    started_at                  TIMESTAMPTZ NOT NULL DEFAULT now(),
    closed_at                   TIMESTAMPTZ,
    closed_reason               TEXT
                                     CHECK (closed_reason IN (
                                         'explicit_stop', 'closing_question_yes',
                                         'auto_close_silence'
                                     )),
    last_activity_at            TIMESTAMPTZ NOT NULL DEFAULT now(),
    closing_question_asked_at   TIMESTAMPTZ,
    closing_question_retries    INT NOT NULL DEFAULT 0,
    -- Set when someone answers the closing question with "not yet": every
    -- due-check below ignores the session until this passes. Without it a
    -- session whose event_date is in the past re-qualifies on the very next
    -- worker tick and the bot asks again forever (R4: "no further closing
    -- question is asked until conditions change").
    closing_question_snoozed_until TIMESTAMPTZ
);

CREATE UNIQUE INDEX IF NOT EXISTS one_active_session_per_chat
    ON sessions (chat_id) WHERE status = 'active';

CREATE TABLE IF NOT EXISTS facts (
    id          BIGSERIAL PRIMARY KEY,
    session_id  BIGINT NOT NULL REFERENCES sessions(id),
    key         TEXT NOT NULL,
    value       TEXT NOT NULL,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_facts_session_key ON facts (session_id, key);

CREATE TABLE IF NOT EXISTS participants (
    id            BIGSERIAL PRIMARY KEY,
    session_id    BIGINT NOT NULL REFERENCES sessions(id),
    user_id       BIGINT,
    display_name  TEXT NOT NULL,
    -- The @username, without the '@', kept apart from display_name because
    -- they identify at different strengths. A username is unique and is what
    -- other people type when they mean this person; a display name is a
    -- first name two people in one chat can share. Someone first written down
    -- from another person's message has only one of the two, so both are
    -- nullable and both are match keys — see set_participant.
    username      TEXT,
    status        TEXT NOT NULL DEFAULT 'unknown'
                       CHECK (status IN ('unknown', 'confirmed', 'maybe', 'declined')),
    responded_at  TIMESTAMPTZ,
    created_at    TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_participants_session ON participants (session_id);
-- The unique index on (session_id, lower(username)) is NOT here. It lives in
-- the ALTER block below, immediately after the ADD COLUMN that creates the
-- column it indexes. Here it would run against a database where participants
-- already exists — CREATE TABLE IF NOT EXISTS is then a no-op, the column has
-- not been added yet, and the whole schema statement fails with
-- `column "username" does not exist`. A fresh database hides that completely,
-- which is exactly how it reached production.

CREATE TABLE IF NOT EXISTS list_items (
    id          BIGSERIAL PRIMARY KEY,
    session_id  BIGINT NOT NULL REFERENCES sessions(id),
    name        TEXT NOT NULL,
    status      TEXT NOT NULL DEFAULT 'pending'
                     CHECK (status IN ('pending', 'checked')),
    added_by    BIGINT,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    checked_at  TIMESTAMPTZ,
    -- Superseded by amount/unit below and kept only for rows written before
    -- them. The reasoning for free text was that parsing "пару бутылок" into
    -- a number invents precision — true, but the cost was worse: the same
    -- amount reached the list as "2 кг", "одну бутылку" and "полкило" and was
    -- shown three different ways. The model now picks the number and a unit
    -- from a fixed vocabulary, and bot/quantity.py writes it out.
    quantity    TEXT,
    -- NULL means nobody said how much; it does not mean one.
    amount      NUMERIC,
    unit        TEXT,
    -- One of the fixed vocabulary in bot/tools/core.py, or NULL until set.
    category    TEXT,
    -- Both kept for the same reason participants keeps user_id and
    -- display_name separately: someone can be named before they are
    -- identified. NULL claimed_by means nobody has taken it.
    claimed_by  TEXT,
    claimed_by_user_id BIGINT
);
CREATE INDEX IF NOT EXISTS idx_list_items_session ON list_items (session_id);
-- One row per item name per session. A shopping list with "огурцы" twice is
-- never what anyone meant, and a model that re-adds an item it already added
-- (observed on gpt-oss-120b) would otherwise quietly corrupt the list.
CREATE UNIQUE INDEX IF NOT EXISTS one_item_name_per_session
    ON list_items (session_id, lower(name));

CREATE TABLE IF NOT EXISTS reminders (
    id              BIGSERIAL PRIMARY KEY,
    session_id      BIGINT NOT NULL REFERENCES sessions(id),
    chat_id         BIGINT NOT NULL,
    target_user_id  BIGINT,
    message         TEXT NOT NULL,
    remind_at       TIMESTAMPTZ NOT NULL,
    -- The wall-clock time as it was asked for ("9 утра"), plus the zone that
    -- was assumed when it had to be guessed. Keeping the local time — rather
    -- than just an offset — is what makes re-anchoring exact once the real
    -- zone is known, including across a DST boundary.
    local_time      TIMESTAMP,
    assumed_timezone TEXT,
    status          TEXT NOT NULL DEFAULT 'pending'
                         CHECK (status IN ('pending', 'sent', 'cancelled', 'failed')),
    -- Delivery attempts. A reminder to someone who has blocked the bot can
    -- never succeed; without a count the worker would retry it every 60s
    -- forever and the queue would never drain.
    attempts        INT NOT NULL DEFAULT 0,
    -- NULL means "not repeating" — this column is the flag, not a companion
    -- boolean. A repeating reminder is one row that moves: remind_at advances
    -- by this many minutes after each delivery instead of new rows piling up,
    -- which is what makes cancelling it a single UPDATE rather than a race
    -- against however many future occurrences already exist.
    repeat_every_minutes INT,
    repeat_until    TIMESTAMPTZ,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    sent_at         TIMESTAMPTZ
);
CREATE INDEX IF NOT EXISTS idx_reminders_due ON reminders (status, remind_at);

-- A roll call is the bot going round the group asking everyone who has not
-- said whether they are coming, three times with a growing gap, then posting
-- who answered and who did not.
--
-- Deliberately not a repeating `reminders` row, though the shape looks close.
-- A reminder carries fixed text; every roll-call round is composed fresh from
-- whoever is still 'unknown' at that moment, and the run ends either after
-- MAX_ROUNDS or the moment nobody is left to ask — neither of which
-- repeat_until can express. The fourth message is a different message again.
--
-- The interval is not stored: it is a pure function of rounds_done (see
-- bot/roll_call.py), so changing the schedule is a code change rather than a
-- migration over live rows.
CREATE TABLE IF NOT EXISTS roll_calls (
    id           BIGSERIAL PRIMARY KEY,
    session_id   BIGINT NOT NULL REFERENCES sessions(id),
    chat_id      BIGINT NOT NULL,
    rounds_done  INT NOT NULL DEFAULT 0,
    next_at      TIMESTAMPTZ NOT NULL,
    status       TEXT NOT NULL DEFAULT 'active'
                      CHECK (status IN ('active', 'done', 'cancelled')),
    created_at   TIMESTAMPTZ NOT NULL DEFAULT now()
);
-- One running roll call per session. Asked twice in a row ("спроси всех" said
-- again a minute later), two rows would post every one of the six messages
-- twice, and the group would read it as the bot malfunctioning.
CREATE UNIQUE INDEX IF NOT EXISTS one_active_roll_call_per_session
    ON roll_calls (session_id) WHERE status = 'active';
CREATE INDEX IF NOT EXISTS idx_roll_calls_due ON roll_calls (status, next_at);

CREATE TABLE IF NOT EXISTS places (
    id           BIGSERIAL PRIMARY KEY,
    session_id   BIGINT NOT NULL REFERENCES sessions(id),
    name         TEXT NOT NULL,
    address      TEXT,
    lat          DOUBLE PRECISION NOT NULL,
    lon          DOUBLE PRECISION NOT NULL,
    -- The phrasing that resolved this place ("поляна Ханания"), as opposed to
    -- the canonical name maps returned ("Hanania Meadow"). Cached lookups match
    -- on either, so asking again the way you asked the first time is a cache
    -- hit rather than a second maps call and a duplicate row (R9).
    query        TEXT,
    resolved_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_places_session ON places (session_id);
-- One row per place name per session: re-resolving the same place must update
-- nothing and insert nothing, not accumulate near-duplicate rows that
-- archive_lookup would later count as separate visits.
CREATE UNIQUE INDEX IF NOT EXISTS one_place_name_per_session
    ON places (session_id, lower(name));

CREATE TABLE IF NOT EXISTS seen_updates (
    update_id  BIGINT PRIMARY KEY,
    seen_at    TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- Per chat, not global: the model chain is a routing decision, and one group
-- changing it for every other group is not a setting, it is a surprise. A
-- chat with no row uses the deployment default.
CREATE TABLE IF NOT EXISTS chat_settings (
    chat_id     BIGINT NOT NULL,
    key         TEXT NOT NULL,
    value       JSONB NOT NULL,
    updated_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_by  BIGINT,
    PRIMARY KEY (chat_id, key)
);

CREATE TABLE IF NOT EXISTS decision_log (
    id          BIGSERIAL PRIMARY KEY,
    chat_id     BIGINT NOT NULL,
    user_id     BIGINT,
    raw_text    TEXT,
    stage       TEXT NOT NULL,
    decision    JSONB NOT NULL,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_decision_log_chat ON decision_log (chat_id, created_at);

CREATE TABLE IF NOT EXISTS pending_confirmations (
    id              BIGSERIAL PRIMARY KEY,
    chat_id         BIGINT NOT NULL,
    session_id      BIGINT REFERENCES sessions(id),
    action_type     TEXT NOT NULL,
    action_params   JSONB NOT NULL,
    proposed_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
    status          TEXT NOT NULL DEFAULT 'pending'
                         CHECK (status IN ('pending', 'confirmed', 'rejected', 'expired'))
);
CREATE INDEX IF NOT EXISTS idx_pending_confirmations_chat
    ON pending_confirmations (chat_id, status);
"""


async def create_pool(dsn: str, *, init=None) -> asyncpg.Pool:
    async def _init_connection(conn):
        # jsonb columns (decision_log.decision, pending_confirmations.action_params)
        # round-trip as plain Python dicts, not raw JSON strings. Codec
        # registration is client-side state on the connection object, so it
        # only needs to happen once, at physical connection creation.
        await conn.set_type_codec(
            "jsonb", encoder=json.dumps, decoder=json.loads, schema="pg_catalog"
        )

    # `init` runs as asyncpg's per-acquire `setup` hook rather than its
    # once-per-physical-connection `init` hook: asyncpg resets session state
    # (e.g. search_path set via `SET`) whenever a connection is released
    # back to the pool, so anything the caller's `init` needs to hold across
    # acquisitions (like pinning a schema) must be re-applied on every
    # acquire.
    return await asyncpg.create_pool(
        dsn, init=_init_connection, setup=init, connection_class=_LoggingConnection
    )


# Columns added after a database may already have been created. `CREATE TABLE
# IF NOT EXISTS` is a no-op on an existing table, so it would never add them;
# these idempotent ALTERs keep an already-provisioned database in step with
# _SCHEMA_SQL. Still plain DDL — no migration framework, per the epic's
# constraints — and safe to run on every startup.
_ALTERS_SQL = """
ALTER TABLE sessions ADD COLUMN IF NOT EXISTS closing_question_snoozed_until TIMESTAMPTZ;
-- A navigator link somebody sent, kept exactly as sent. Not in `places`:
-- that table's lat/lon are NOT NULL and a short link carries neither, and
-- expanding one to find out is the thing this column exists to stop the bot
-- doing. One event has one place, so one column rather than a table.
ALTER TABLE sessions ADD COLUMN IF NOT EXISTS place_url TEXT;
-- Where chats.timezone came from: 'stated' when a human said it, 'lookup'
-- when it was guessed from a place's coordinates. NULL for rows written
-- before this column existed, which are treated as guesses — the safe
-- reading, since a guess is what most of them were.
--
-- Without it a guess could never be corrected: the rule was "never
-- overwrite", written to protect a human's word, and it protected one bad
-- lookup just as firmly. Live, a place called "Бен & Co" resolved to a
-- jeweller in Pretoria, the chat became Africa/Johannesburg, and saying "мы
-- в Тель-Авиве" afterwards changed the place but not the zone.
ALTER TABLE chats ADD COLUMN IF NOT EXISTS timezone_source TEXT
    CHECK (timezone_source IN ('stated', 'lookup'));
-- Learned lazily from get_chat_administrators: the API has no notification
-- for it, so it is filled in the first time a chat is synced and left alone
-- afterwards. NULL means "not looked up yet", not "no creator".
ALTER TABLE chats ADD COLUMN IF NOT EXISTS creator_user_id BIGINT;
ALTER TABLE places ADD COLUMN IF NOT EXISTS query TEXT;
ALTER TABLE reminders ADD COLUMN IF NOT EXISTS attempts INT NOT NULL DEFAULT 0;
ALTER TABLE chats ADD COLUMN IF NOT EXISTS timezone TEXT;
ALTER TABLE reminders ADD COLUMN IF NOT EXISTS local_time TIMESTAMP;
ALTER TABLE reminders ADD COLUMN IF NOT EXISTS assumed_timezone TEXT;
-- Widening a CHECK needs the old one dropped first; there is no
-- ADD CONSTRAINT IF NOT EXISTS. Both statements are idempotent together.
ALTER TABLE reminders DROP CONSTRAINT IF EXISTS reminders_status_check;
ALTER TABLE reminders ADD CONSTRAINT reminders_status_check
    CHECK (status IN ('pending', 'sent', 'cancelled', 'failed'));
-- "maybe" is a hedged reply ("может быть", "постараюсь") — distinct from
-- "unknown", which means nobody has answered at all. Conflating the two
-- would make the ❓/◻️ icon distinction the user asked for impossible to draw.
ALTER TABLE participants DROP CONSTRAINT IF EXISTS participants_status_check;
ALTER TABLE participants ADD CONSTRAINT participants_status_check
    CHECK (status IN ('unknown', 'confirmed', 'maybe', 'declined'));
ALTER TABLE reminders ADD COLUMN IF NOT EXISTS repeat_every_minutes INT;
ALTER TABLE reminders ADD COLUMN IF NOT EXISTS repeat_until TIMESTAMPTZ;
-- TEXT, not a number: people say "пару бутылок", "кг", "штук 5", and parsing
-- that into a number+unit would invent precision nobody gave (R4). NULL means
-- nobody said how much. claimed_by/claimed_by_user_id split the same way
-- participants does: someone can be named before they are identified.
ALTER TABLE list_items ADD COLUMN IF NOT EXISTS quantity TEXT;
ALTER TABLE list_items ADD COLUMN IF NOT EXISTS category TEXT;
ALTER TABLE list_items ADD COLUMN IF NOT EXISTS claimed_by TEXT;
ALTER TABLE list_items ADD COLUMN IF NOT EXISTS claimed_by_user_id BIGINT;
-- Amount split into a number and a unit, replacing the free-text quantity.
-- That column stored whatever phrasing reached it — "2 кг", "одну бутылку",
-- "полкило" — so the same amount read three different ways in one list.
-- quantity is kept, not dropped: rows written before this still carry their
-- text, and bot/list_render.py falls back to it when amount is NULL.
ALTER TABLE list_items ADD COLUMN IF NOT EXISTS amount NUMERIC;
ALTER TABLE list_items ADD COLUMN IF NOT EXISTS unit TEXT;
-- One item can hold several amounts in different units: asked to add a
-- bottle of wine when a crate is already listed, the answer is "1 ящ. +
-- 1 бут.", not one of the two. A single amount/unit pair cannot say that, so
-- those two columns become the legacy read path and this is where new
-- amounts go: [{"amount": 1, "unit": "ящик"}, {"amount": 1, "unit": "бутылка"}].
ALTER TABLE list_items ADD COLUMN IF NOT EXISTS amounts JSONB;
-- bot.group_info's last-seen bookkeeping (R7): NULL title_seen/description_seen
-- means "never checked", not "checked and found nothing" — that distinction is
-- what lets a first sighting store the values without announcing them as a
-- change. member_count lands here too, even though it is Task 6's column in
-- the story doc: Task 4's worker.group_sync writes it on every pass, which
-- runs before Task 6 in build order, so the column has to exist by then or
-- worker.group_sync's own tests fail against the real schema.
ALTER TABLE chats ADD COLUMN IF NOT EXISTS title_seen TEXT;
ALTER TABLE chats ADD COLUMN IF NOT EXISTS description_seen TEXT;
ALTER TABLE chats ADD COLUMN IF NOT EXISTS info_checked_at TIMESTAMPTZ;
ALTER TABLE chats ADD COLUMN IF NOT EXISTS member_count INT;
-- How many times a repeating reminder has actually gone out. repeat_until
-- alone bounded a series only in time, so "каждые 5 минут до завтра" was 288
-- messages the group never meant to ask for; three is the ceiling now, and it
-- has to be counted because the end time cannot express it.
ALTER TABLE reminders ADD COLUMN IF NOT EXISTS deliveries INT NOT NULL DEFAULT 0;
-- See the participants table above: the @username is a second, stronger
-- identity key, and the partial unique index is what makes a second write for
-- the same person an update rather than a duplicate row.
ALTER TABLE participants ADD COLUMN IF NOT EXISTS username TEXT;
CREATE UNIQUE INDEX IF NOT EXISTS one_username_per_session
    ON participants (session_id, lower(username)) WHERE username IS NOT NULL;
-- Rows written before the column existed put the @handle in display_name,
-- because a message like "@poohpeer не участвует" contains nothing else. Left
-- there it is invisible to every lookup, so the duplicate those rows are half
-- of could never heal: link_identity searches `username`, finds nothing, and
-- the same person stays twice on the roster for ever.
--
-- This moves the handle into its own column and nothing more. It does not
-- merge anybody — merging rows with conflicting answers is the group's call,
-- not a migration's. It only makes the rows recognisable, so the ordinary
-- path (that person's next message) can do the merge with real evidence.
--
-- The NOT EXISTS guard is the unique index: a session that somehow already
-- has the handle on another row keeps it there, and this row is left alone
-- rather than failing the whole migration.
UPDATE participants p SET username = lower(substring(display_name from 2))
WHERE p.username IS NULL
  AND p.display_name ~ '^@[A-Za-z0-9_]{4,32}$'
  AND NOT EXISTS (
      SELECT 1 FROM participants o
      WHERE o.session_id = p.session_id AND o.id <> p.id
        AND lower(o.username) = lower(substring(p.display_name from 2))
  );
"""


async def init_db(pool: asyncpg.Pool) -> None:
    async with pool.acquire() as conn:
        await conn.execute(_SCHEMA_SQL)
        await conn.execute(_ALTERS_SQL)
