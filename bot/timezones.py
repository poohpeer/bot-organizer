"""Per-chat timezone resolution.

Telegram exposes no timezone: a message carries a UTC `date` and nothing else.
So the zone is resolved in layers, cheapest first — an environment default that
always works, a per-chat override once anything better is known, and a free
lookup from the event's coordinates whenever a place gets resolved.

The important invariant is that conversion happens exactly once, at the point a
local wall-clock time is written down. Everything stored is absolute UTC, so
the worker never has to know about zones at all.
"""

import logging
import os
from datetime import date, datetime, timezone
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import httpx

log = logging.getLogger(__name__)

# Covers every chat that has nothing better recorded, which in a private bot is
# usually all of them. Validated at import: a typo here would otherwise surface
# much later as a wrong reminder time rather than a startup failure.
DEFAULT_TIMEZONE = os.environ.get("DEFAULT_TIMEZONE", "Asia/Jerusalem")
try:
    ZoneInfo(DEFAULT_TIMEZONE)
except (ZoneInfoNotFoundError, ValueError) as e:
    raise RuntimeError(f"DEFAULT_TIMEZONE={DEFAULT_TIMEZONE!r} is not a valid IANA zone") from e

_TIMEZONE_LOOKUP_URL = "https://api.open-meteo.com/v1/forecast"
_LOOKUP_TIMEOUT = 10.0


def is_valid_timezone(name) -> bool:
    if not isinstance(name, str) or not name:
        return False
    try:
        ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError):
        return False
    return True


async def stored_timezone(pool, chat_id: int) -> str | None:
    """The zone actually recorded for this chat, or None if nobody knows yet.

    Distinct from `chat_timezone`, which always returns something usable: the
    caller needs to tell "we know" from "we are assuming", so the bot can say
    which it is instead of quietly guessing.
    """
    name = await pool.fetchval("SELECT timezone FROM chats WHERE chat_id = $1", chat_id)
    return name if name is not None and is_valid_timezone(name) else None


async def reanchor_pending_reminders(pool, chat_id: int, name: str) -> int:
    """Re-interpret reminders that were scheduled under an assumed zone.

    Recomputed from the stored wall-clock time rather than shifted by an offset
    delta, so a reminder that straddles a DST change still lands at the hour it
    was asked for. Only pending ones: something already delivered cannot be
    un-delivered.
    """
    tz = ZoneInfo(name)
    rows = await pool.fetch(
        """
        SELECT id, local_time FROM reminders
        WHERE chat_id = $1 AND status = 'pending'
          AND local_time IS NOT NULL AND assumed_timezone IS DISTINCT FROM $2
        """,
        chat_id, name,
    )
    for row in rows:
        await pool.execute(
            "UPDATE reminders SET remind_at = $2, assumed_timezone = NULL WHERE id = $1",
            row["id"], to_utc(row["local_time"], tz),
        )
    return len(rows)


async def chat_timezone(pool, chat_id: int) -> ZoneInfo:
    """The chat's zone, falling back to the configured default.

    A stored value that is no longer a valid IANA name (a renamed zone, a bad
    manual entry) falls back rather than raising: being an hour off is a far
    better failure than the whole reminder path breaking.
    """
    name = await pool.fetchval("SELECT timezone FROM chats WHERE chat_id = $1", chat_id)
    if name is not None and is_valid_timezone(name):
        return ZoneInfo(name)
    if name is not None:
        log.warning("Chat %s has unusable timezone %r, using %s", chat_id, name, DEFAULT_TIMEZONE)
    return ZoneInfo(DEFAULT_TIMEZONE)


async def known_to_postgres(pool, name: str) -> bool:
    """Whether Postgres will accept this name in `AT TIME ZONE`.

    Python and Postgres do not ship the same list: zoneinfo knows 599 zones to
    Postgres's 487, and the 113 extras are mostly legacy aliases —
    "America/Buenos_Aires", "US/Hawaii" — exactly what a model reaches for.
    Storing one is worse than rejecting it: the closing-question query would
    then raise for the whole batch, so one chat's bad zone silences the bot
    everywhere.
    """
    return await pool.fetchval("SELECT EXISTS (SELECT 1 FROM pg_timezone_names WHERE name = $1)", name)


async def set_chat_timezone(pool, chat_id: int, name: str) -> bool:
    """Record a chat's zone. Returns False for anything not a real IANA name,
    so a hallucinated "MSK" or "UTC+3" is refused rather than stored — and for
    anything Postgres would later choke on."""
    if not is_valid_timezone(name) or not await known_to_postgres(pool, name):
        return False
    await pool.execute("UPDATE chats SET timezone = $2 WHERE chat_id = $1", chat_id, name)
    return True


async def learn_timezone_from_coordinates(pool, chat_id: int, lat: float, lon: float) -> str | None:
    """Fill in a chat's zone from a resolved place, if it has none yet.

    Deliberately called from code on place resolution rather than left to the
    model: piggybacking on `weather_lookup` would only work when the model
    happened to check the weather, which is neither guaranteed nor predictable.

    Never overwrites an existing value — an explicit human statement or an
    earlier resolution outranks this — and never raises: failing to learn a
    zone must not fail the place lookup that triggered it.
    """
    existing = await pool.fetchval("SELECT timezone FROM chats WHERE chat_id = $1", chat_id)
    if existing is not None:
        return existing

    try:
        async with httpx.AsyncClient(timeout=_LOOKUP_TIMEOUT) as client:
            resp = await client.get(
                _TIMEZONE_LOOKUP_URL,
                params={"latitude": lat, "longitude": lon, "timezone": "auto"},
            )
            resp.raise_for_status()
            name = resp.json().get("timezone")
    except Exception:
        log.warning("Timezone lookup failed for %s,%s", lat, lon, exc_info=True)
        return None

    if not is_valid_timezone(name) or not await known_to_postgres(pool, name):
        return None
    await pool.execute("UPDATE chats SET timezone = $2 WHERE chat_id = $1", chat_id, name)
    return name


def to_utc(when: datetime, tz: ZoneInfo) -> datetime:
    """Interpret a wall-clock time in the chat's zone and return absolute UTC.

    A naive datetime is what the model produces for "напомни в 9 утра": it means
    9am where the group is, not 9am UTC. Reading it as UTC is what made a UTC+3
    group's reminders fire three hours late.
    """
    if when.tzinfo is None:
        when = when.replace(tzinfo=tz)
    return when.astimezone(timezone.utc)


def local_date(tz: ZoneInfo) -> date:
    return datetime.now(tz).date()
