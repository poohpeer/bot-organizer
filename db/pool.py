import json

import asyncpg

_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS chats (
    chat_id    BIGINT PRIMARY KEY,
    title      TEXT,
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
    closing_question_retries    INT NOT NULL DEFAULT 0
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

CREATE TABLE IF NOT EXISTS reminders (
    id              BIGSERIAL PRIMARY KEY,
    session_id      BIGINT NOT NULL REFERENCES sessions(id),
    chat_id         BIGINT NOT NULL,
    target_user_id  BIGINT,
    message         TEXT NOT NULL,
    remind_at       TIMESTAMPTZ NOT NULL,
    status          TEXT NOT NULL DEFAULT 'pending'
                         CHECK (status IN ('pending', 'sent', 'cancelled')),
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
    resolved_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_places_session ON places (session_id);

CREATE TABLE IF NOT EXISTS proactive_suggestions (
    id            BIGSERIAL PRIMARY KEY,
    chat_id       BIGINT NOT NULL,
    topic_key     TEXT NOT NULL,
    suggested_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    response      TEXT CHECK (response IN ('accepted', 'declined', 'ignored'))
);
CREATE INDEX IF NOT EXISTS idx_proactive_chat_time
    ON proactive_suggestions (chat_id, suggested_at);
CREATE INDEX IF NOT EXISTS idx_proactive_topic
    ON proactive_suggestions (chat_id, topic_key, suggested_at);

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
    return await asyncpg.create_pool(dsn, init=_init_connection, setup=init)


async def init_db(pool: asyncpg.Pool) -> None:
    async with pool.acquire() as conn:
        await conn.execute(_SCHEMA_SQL)
