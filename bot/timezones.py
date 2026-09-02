"""Per-chat timezone resolution.

Telegram exposes no timezone: a message carries a UTC `date` and nothing else.
So the zone is resolved in layers, cheapest first — an environment default that
always works, a per-chat override once anything better is known, and a free
lookup from the event's coordinates whenever a place gets resolved.

People are not all in the chat's zone, so a person can have one of their own
(`users.timezone`), and it is used for two things: a reminder addressed to
them is read in *their* wall clock, and the group creator's zone stands in for
the whole chat when nobody has stated the chat's. See effective_timezone for
the full order of precedence.

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


async def effective_timezone(pool, chat_id: int) -> str | None:
    """The zone this chat should be read in, or None if nobody knows yet.

    Distinct from `chat_timezone`, which always returns something usable: the
    caller needs to tell "we know" from "we are assuming", so the bot can say
    which it is instead of quietly guessing.

    Three sources, strongest first:

    1. What a human said about *this chat* ("мы по Москве"). Nothing outranks
       a statement made about the group itself.
    2. The group creator's own stated zone. Whoever made the group is the
       best available stand-in for where it is, and unlike a place lookup it
       is a person's word rather than a guess.
    3. Whatever was guessed from a resolved place's coordinates.

    The creator sits above the guess and below the statement on purpose. A
    guess is what put a chat in Pretoria because a place called "Бен & Co"
    resolved to a jeweller there; a creator who has said where they are is
    strictly better information than that. But a group that has explicitly
    said where *it* is has said something the creator's own whereabouts
    cannot override — the creator may simply be travelling.
    """
    row = await pool.fetchrow(
        """
        SELECT c.timezone, c.timezone_source, u.timezone AS creator_timezone
        FROM chats c LEFT JOIN users u ON u.user_id = c.creator_user_id
        WHERE c.chat_id = $1
        """,
        chat_id,
    )
    if row is None:
        return None
    stated = row["timezone"] if row["timezone_source"] == "stated" else None
    for name in (stated, row["creator_timezone"], row["timezone"]):
        if name is not None and is_valid_timezone(name):
            return name
    return None


# The name this was called before a chat had more than one possible source.
stored_timezone = effective_timezone


async def user_timezone(pool, user_id) -> str | None:
    """The zone this person stated for themselves, or None.

    Only ever set from a human saying so: there is no coordinate lookup here
    on purpose. A location someone shares is almost always the venue, not
    where they are standing, so learning a personal zone from one would be
    guessing about a person — the exact mistake that put a whole chat in
    South Africa.
    """
    if user_id is None:
        return None
    name = await pool.fetchval("SELECT timezone FROM users WHERE user_id = $1", user_id)
    return name if name is not None and is_valid_timezone(name) else None


async def set_user_timezone(pool, user_id: int, name: str) -> bool:
    """Record where a person is. Refuses anything not a real IANA name, and
    anything Postgres would later choke on, for the same reasons
    set_chat_timezone does."""
    if not is_valid_timezone(name) or not await known_to_postgres(pool, name):
        return False
    await pool.execute(
        """
        INSERT INTO users (user_id, timezone) VALUES ($1, $2)
        ON CONFLICT (user_id) DO UPDATE SET timezone = EXCLUDED.timezone, updated_at = now()
        """,
        user_id, name,
    )
    return True


async def set_chat_creator(pool, chat_id: int, user_id: int) -> None:
    """Remember who created a group, so their zone can stand in for its own.

    Upsert rather than UPDATE: the bot learns this the moment it is added to
    a group, which can be before the chat has ever been written down. An
    empty chats row is bookkeeping, not tracking — nothing downstream reads
    one as consent to organize anything.
    """
    await pool.execute(
        """
        INSERT INTO chats (chat_id, creator_user_id) VALUES ($1, $2)
        ON CONFLICT (chat_id) DO UPDATE SET creator_user_id = EXCLUDED.creator_user_id
        """,
        chat_id, user_id,
    )


async def chats_created_by(pool, user_id) -> list[int]:
    """Every chat whose zone this person's own zone might now speak for."""
    if user_id is None:
        return []
    rows = await pool.fetch("SELECT chat_id FROM chats WHERE creator_user_id = $1", user_id)
    return [r["chat_id"] for r in rows]


async def _reanchor(pool, rows, name: str) -> int:
    """Recompute each reminder's instant from its stored wall clock.

    Recomputed rather than shifted by an offset delta, so a reminder that
    straddles a DST change still lands at the hour it was asked for. Returns
    how many actually moved — a row already sitting at the right instant is
    rewritten harmlessly but is not something to report as corrected.
    """
    tz = ZoneInfo(name)
    moved = 0
    for row in rows:
        when = to_utc(row["local_time"], tz)
        if when != row["remind_at"]:
            moved += 1
        await pool.execute(
            "UPDATE reminders SET remind_at = $2, assumed_timezone = NULL WHERE id = $1",
            row["id"], when,
        )
    return moved


async def reanchor_pending_reminders(pool, chat_id: int, name: str) -> int:
    """Re-interpret a chat's pending reminders in a newly-known zone.

    Only pending ones: something already delivered cannot be un-delivered.

    Skips anyone whose own zone decides their reminders: "напомни Васе в 9"
    means nine o'clock where Вася is, and the chat learning where *it* is
    says nothing about that. Without the exclusion, setting the group's zone
    would silently drag every personal reminder onto the group's clock.
    """
    rows = await pool.fetch(
        """
        SELECT id, local_time, remind_at FROM reminders r
        WHERE r.chat_id = $1 AND r.status = 'pending' AND r.local_time IS NOT NULL
          AND NOT EXISTS (
              SELECT 1 FROM users u
              WHERE u.user_id = r.target_user_id AND u.timezone IS NOT NULL
          )
        """,
        chat_id,
    )
    return await _reanchor(pool, rows, name)


async def reanchor_user_reminders(pool, user_id: int, name: str) -> int:
    """Re-interpret everything addressed to one person, in every chat.

    Not scoped to a chat on purpose: a person saying where they are corrects
    their own reminders wherever they were asked for.
    """
    rows = await pool.fetch(
        """
        SELECT id, local_time, remind_at FROM reminders
        WHERE target_user_id = $1 AND status = 'pending' AND local_time IS NOT NULL
        """,
        user_id,
    )
    return await _reanchor(pool, rows, name)


async def chat_timezone(pool, chat_id: int) -> ZoneInfo:
    """The chat's zone, falling back to the configured default.

    A stored value that is no longer a valid IANA name (a renamed zone, a bad
    manual entry) falls back rather than raising: being an hour off is a far
    better failure than the whole reminder path breaking.
    """
    name = await effective_timezone(pool, chat_id)
    if name is not None:
        return ZoneInfo(name)
    stored = await pool.fetchval("SELECT timezone FROM chats WHERE chat_id = $1", chat_id)
    if stored is not None:
        log.warning("Chat %s has unusable timezone %r, using %s", chat_id, stored, DEFAULT_TIMEZONE)
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


async def set_chat_timezone(pool, chat_id: int, name: str, *, source: str = "stated") -> bool:
    """Record a chat's zone. Returns False for anything not a real IANA name,
    so a hallucinated "MSK" or "UTC+3" is refused rather than stored — and for
    anything Postgres would later choke on.

    `source` is how it was arrived at: 'stated' when a human said where they
    are, 'lookup' when it was guessed from coordinates. It decides what a
    later guess is allowed to replace — see learn_timezone_from_coordinates.
    """
    if not is_valid_timezone(name) or not await known_to_postgres(pool, name):
        return False
    await pool.execute(
        "UPDATE chats SET timezone = $2, timezone_source = $3 WHERE chat_id = $1",
        chat_id, name, source,
    )
    return True


async def learn_timezone_from_coordinates(pool, chat_id: int, lat: float, lon: float) -> str | None:
    """Set a chat's zone from a resolved place, unless a human already said it.

    Deliberately called from code on place resolution rather than left to the
    model, which would do it only on the turns it happened to think the zone
    mattered — neither guaranteed nor predictable.

    Never overwrites what a human stated: someone saying "мы по Москве" knows
    better than the coordinates of a restaurant they looked up.

    An earlier *guess* is a different matter, and it used to be protected
    just as firmly. Live: a place typed as "Бен & Co" resolved to a jeweller
    in Pretoria, the chat became Africa/Johannesburg, and the group saying
    "мы в Тель-Авиве" afterwards moved the place but not the zone — leaving
    every reminder an hour out, with nothing on screen to explain it. A later
    lookup now replaces an earlier one, because the newer one was made with
    more of the conversation behind it.

    A row with no recorded source predates the column and is treated as a
    guess: that is what most of them were, and the alternative is a chat
    stuck on a guess forever.

    Never raises: failing to learn a zone must not fail the place lookup that
    triggered it.
    """
    row = await pool.fetchrow(
        "SELECT timezone, timezone_source FROM chats WHERE chat_id = $1", chat_id
    )
    if row is not None and row["timezone"] is not None and row["timezone_source"] == "stated":
        return row["timezone"]

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
    await pool.execute(
        "UPDATE chats SET timezone = $2, timezone_source = 'lookup' WHERE chat_id = $1",
        chat_id, name,
    )
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
