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
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
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
    status        TEXT NOT NULL DEFAULT 'unknown'
                       CHECK (status IN ('unknown', 'confirmed', 'declined')),
    responded_at  TIMESTAMPTZ,
    created_at    TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_participants_session ON participants (session_id);

CREATE TABLE IF NOT EXISTS list_items (
    id          BIGSERIAL PRIMARY KEY,
    session_id  BIGINT NOT NULL REFERENCES sessions(id),
    name        TEXT NOT NULL,
    status      TEXT NOT NULL DEFAULT 'pending'
                     CHECK (status IN ('pending', 'checked')),
    added_by    BIGINT,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    checked_at  TIMESTAMPTZ
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
ALTER TABLE reminders ADD COLUMN IF NOT EXISTS repeat_every_minutes INT;
ALTER TABLE reminders ADD COLUMN IF NOT EXISTS repeat_until TIMESTAMPTZ;
"""


async def init_db(pool: asyncpg.Pool) -> None:
    async with pool.acquire() as conn:
        await conn.execute(_SCHEMA_SQL)
        await conn.execute(_ALTERS_SQL)
