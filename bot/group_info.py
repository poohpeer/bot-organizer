"""Read a group's own title/description/member count, and remember what was
last seen so a repeat check can tell "nothing changed" from "something did".

See ../docs/tasks/0002-live-chat-feedback/EPIC.md's "What the Telegram Bot API
cannot do": there is no way to list a group's members, so this module — and
worker.group_sync, which drives it — builds the roster the API actually
allows instead of the one that was asked for.
"""

import logging
from datetime import date

from google.genai import types

from bot.ai.classify import extract

log = logging.getLogger(__name__)


async def fetch(telegram_bot, chat_id: int) -> dict:
    """The group's own title, description and member count, or {} on any
    failure. Never raises — this runs on a timer for every chat with an
    active session, and one unreachable chat (kicked, banned, network blip)
    must not take the rest of the poll down with it, the same discipline
    bot.ai.classify.extract follows for the model chain.
    """
    try:
        chat = await telegram_bot.get_chat(chat_id)
        member_count = await telegram_bot.get_chat_member_count(chat_id)
        return {
            "title": chat.title,
            "description": chat.description,
            "member_count": member_count,
        }
    except Exception:
        log.warning("group_info.fetch failed for chat %s", chat_id, exc_info=True)
        return {}


async def changes_since_last_seen(pool, chat_id: int, info: dict) -> dict:
    """Compare fetched info against what was last stored, then store it.

    Comparison and storage happen in one call so a caller that announces a
    change on `True` and then falls through to updating `_seen` can't get the
    two steps out of order — a bug there is the one way this becomes the same
    announcement every poll (R7's second criterion).

    `info_checked_at IS NULL` — not merely "no chats row" — is what marks a
    first sighting: title_seen/description_seen are NULL to begin with too,
    so an ordinary "no change" comparison against a never-checked chat would
    otherwise read *every* field as having changed from NULL, and announce
    itself the moment the worker first polls it.
    """
    title = info.get("title")
    description = info.get("description")

    row = await pool.fetchrow(
        "SELECT title_seen, description_seen, info_checked_at FROM chats WHERE chat_id = $1",
        chat_id,
    )
    first_sighting = row is None or row["info_checked_at"] is None
    previous = {
        "title": row["title_seen"] if row is not None else None,
        "description": row["description_seen"] if row is not None else None,
    }

    await pool.execute(
        """
        INSERT INTO chats (chat_id, title_seen, description_seen, info_checked_at)
        VALUES ($1, $2, $3, now())
        ON CONFLICT (chat_id) DO UPDATE SET
            title_seen = EXCLUDED.title_seen,
            description_seen = EXCLUDED.description_seen,
            info_checked_at = EXCLUDED.info_checked_at
        """,
        chat_id, title, description,
    )

    return {
        "title_changed": not first_sighting and title != previous["title"],
        "description_changed": not first_sighting and description != previous["description"],
        "previous": previous,
    }


_EVENT_SCHEMA = types.Schema(
    type=types.Type.OBJECT,
    properties={
        "activity_type": types.Schema(
            type=types.Type.STRING,
            description="Short label for the activity/event named in the title or "
            "description (e.g. 'picnic', 'trip'). Omit if none is actually named.",
        ),
        "event_date": types.Schema(
            type=types.Type.STRING,
            description="ISO-8601 date (YYYY-MM-DD) if a date is stated anywhere in "
            "the title or description, otherwise omit — never guess one.",
        ),
        "place": types.Schema(
            type=types.Type.STRING,
            description="Where the event is, if stated. Omit if not.",
        ),
    },
)

_EXTRACT_INSTRUCTION_TEMPLATE = (
    "A Telegram group's own title and/or description may name an event this "
    "group is organizing. Extract only what is actually stated: the activity, "
    "its date, and its place. Leave a field out entirely if the text does not "
    "say it — never invent an activity, date or place that isn't there.\n"
    "Today's local date for this group is {today}. If a date is given without "
    "a year (e.g. '4 июля'), resolve it as the next upcoming occurrence of "
    "that day relative to today, not a date from any other year."
)


async def extract_event(title, description, today: date) -> dict:
    """What the title/description say about the event, or all-None if they
    say nothing. Uses the cheap classifier model (bot.ai.classify.extract) —
    this runs on a timer for every chat, and the epic's constraint is that
    filter-style checks never spend the primary model.

    `today` has to come from the caller rather than being read in here: this
    module has no chat_id-to-timezone path of its own, and resolving "4 июля"
    against the server's own date/zone instead of the group's would be the
    same bug 0001's S11 fixed for reminders (see bot.timezones.local_date).
    """
    instruction = _EXTRACT_INSTRUCTION_TEMPLATE.format(today=today.isoformat())
    text = f"Title: {title or '(none)'}\nDescription: {description or '(none)'}"
    extracted = await extract(instruction, text, _EVENT_SCHEMA)
    return {
        "activity_type": extracted.get("activity_type") or None,
        "event_date": extracted.get("event_date") or None,
        "place": extracted.get("place") or None,
    }
