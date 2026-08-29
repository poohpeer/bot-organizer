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

import bot.tools.core as core_tools
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


async def changed_fields(pool, chat_id: int, info: dict) -> set[str]:
    """Which of title/description differ from what was last recorded.

    Read-only, deliberately. It used to compare and store in one call, so
    that a caller could not get the two steps out of order — but that made
    the change consumed the instant it was noticed, before anything was done
    about it. Live, the title moved to "Маленькая прага 31/8", this marked it
    seen, the classifier chain then answered 400, and the change was gone:
    the next pass saw no difference and the event never learnt the new place.
    Recording is now record_as_seen, called once the work has actually
    happened.

    `info_checked_at IS NULL` — not merely "no chats row" — is what marks a
    first sighting: title_seen/description_seen are NULL to begin with too,
    so an ordinary comparison against a never-checked chat would read *every*
    field as changed the moment the worker first polls it.
    """
    row = await pool.fetchrow(
        "SELECT title_seen, description_seen, info_checked_at FROM chats WHERE chat_id = $1",
        chat_id,
    )
    if row is None or row["info_checked_at"] is None:
        return set()

    changed = set()
    if info.get("title") != row["title_seen"]:
        changed.add("title")
    if info.get("description") != row["description_seen"]:
        changed.add("description")
    return changed


async def record_as_seen(pool, chat_id: int, info: dict) -> None:
    """Remember this title/description as handled.

    Called after the work a change triggered has been done, never before —
    see changed_fields. A first sighting goes through here too, which is what
    makes info_checked_at non-NULL and turns future comparisons on.
    """
    await pool.execute(
        """
        INSERT INTO chats (chat_id, title_seen, description_seen, info_checked_at)
        VALUES ($1, $2, $3, now())
        ON CONFLICT (chat_id) DO UPDATE SET
            title_seen = EXCLUDED.title_seen,
            description_seen = EXCLUDED.description_seen,
            info_checked_at = EXCLUDED.info_checked_at
        """,
        chat_id, info.get("title"), info.get("description"),
    )


# What counts as a place. An enum rather than a free-text field or a
# boolean: the model has to pick one of these, and the code decides on the
# value it picked rather than on prose it wrote. "нет" is the answer for a
# title that is a group's name, a joke or a mood — "Друзья", "Наши",
# "Отдыхаем" — none of which are anywhere anyone can go.
#
# It has to be the model's judgement: resolving the text through maps would
# look like enforcement and is not, because maps returns a result for almost
# any string. A constrained answer the code acts on is as close as this gets.
PLACE_KINDS = ("город", "адрес", "заведение", "природа", "нет")
NOT_A_PLACE = "нет"

_EVENT_SCHEMA = types.Schema(
    type=types.Type.OBJECT,
    properties={
        "activity_type": types.Schema(
            type=types.Type.STRING,
            description="Short label for the activity/event named in the title or "
            "description (e.g. 'picnic', 'trip'). Empty string if none is named.",
        ),
        "event_date": types.Schema(
            type=types.Type.STRING,
            description="ISO-8601 date (YYYY-MM-DD) if a date is stated anywhere in "
            "the title or description — including a bare '20/11' or '31/8', which "
            "are day/month. Empty string if no date is stated; never guess one.",
        ),
        "place": types.Schema(
            type=types.Type.STRING,
            description="The place itself, exactly as written and without the date: "
            "'Море 20/11' is 'Море', 'Маленькая прага 31/8' is 'Маленькая прага'. "
            "Empty string unless the text names somewhere the event actually "
            "happens.",
        ),
        "place_kind": types.Schema(
            type=types.Type.STRING,
            enum=list(PLACE_KINDS),
            description="What kind of place `place` is: 'город' a city or town, "
            "'адрес' a street address, 'заведение' a named venue — restaurant, bar, "
            "hall, park with a name, 'природа' a beach, forest, lake or similar. "
            "Answer 'нет' when the text names no place at all — a group's own name, "
            "a joke or a mood is not a place: 'Друзья', 'Наши', 'Отдыхаем' are all "
            "'нет'. Answering anything but 'нет' means the group could go there.",
        ),
    },
)

_EXTRACT_INSTRUCTION_TEMPLATE = (
    "A Telegram group's own title and/or description may name an event this "
    "group is organizing. Extract only what is actually stated: the activity, "
    "its date, and its place. Every field must be present in your answer — "
    "use an empty string for anything the text does not say, and never invent "
    "an activity, date or place that isn't there.\n"
    "Group titles are commonly a place and a date together: 'Море 20/11' is "
    "the place Море on 20 November, 'Маленькая прага 31/8' is the venue "
    "Маленькая прага on 31 August. Split them — the date never belongs in "
    "`place`.\n"
    "Today's local date for this group is {today}. If a date is given without "
    "a year (e.g. '4 июля', '20/11'), resolve it as the next upcoming "
    "occurrence of that day relative to today, not a date from any other "
    "year. A bare numeric date is day/month, not month/day."
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
    if not extracted:
        # extract() fails closed, returning {} for a transport error, a
        # refused request or an unparseable answer alike. With the strict
        # schema a successful call always carries every key, so an empty
        # dict means nobody answered — distinct from "answered, and the text
        # says nothing", and the caller has to be able to tell them apart or
        # it will mark a change handled that was never read.
        return {"answered": False, "activity_type": None, "event_date": None,
                "place": None, "place_kind": NOT_A_PLACE}

    place = extracted.get("place") or None
    kind = extracted.get("place_kind") or NOT_A_PLACE
    if kind == NOT_A_PLACE or kind not in PLACE_KINDS:
        # Not somewhere anyone can go, or a kind nobody asked for. Either
        # way the place is dropped here rather than downstream, so no caller
        # has to remember this rule. An unrecognised value is treated as
        # "no": a place is only stored on a positive, known answer.
        place = None
    return {
        "answered": True,
        "activity_type": extracted.get("activity_type") or None,
        "event_date": extracted.get("event_date") or None,
        "place": place,
        "place_kind": kind,
    }


def parse_iso_date(value) -> date | None:
    """An ISO date, or None for anything that is not one.

    The classifier is asked for ISO-8601 and is not guaranteed to comply, and
    a malformed date must not raise out of a timer pass or a tool call. Was
    copied verbatim in three places (worker.group_sync, bot.tools.composed,
    bot.router); one copy here so a fix reaches all of them.
    """
    if not isinstance(value, str):
        return None
    try:
        return date.fromisoformat(value)
    except ValueError:
        return None


async def apply_event(pool, session_id: int, event: dict) -> dict:
    """Write the date and place the chat's own title/description state, and
    only when they differ from what the session already holds.

    Writing unconditionally is not harmless. facts rows are append-only —
    get_facts takes the newest per key — so re-recording an unchanged place
    every time a title is touched grows a pile of identical rows, and the
    same UPDATE on sessions makes a no-op look like an edit to anyone reading
    the table.

    Returns what actually changed, so a caller can tell "read it and nothing
    was new" from "read it and nothing was there".
    """
    row = await pool.fetchrow("SELECT event_date FROM sessions WHERE id = $1", session_id)
    if row is None:
        return {}

    changed: dict = {}

    parsed = parse_iso_date(event.get("event_date"))
    if parsed is not None and parsed != row["event_date"]:
        await pool.execute(
            "UPDATE sessions SET event_date = $2 WHERE id = $1", session_id, parsed
        )
        changed["event_date"] = parsed.isoformat()

    place = event.get("place")
    if place:
        current = await pool.fetchval(
            "SELECT value FROM facts WHERE session_id = $1 AND key = 'place' "
            "ORDER BY created_at DESC LIMIT 1",
            session_id,
        )
        if place != current:
            await core_tools.remember_fact(pool, session_id, "place", place)
            changed["place"] = place

    return changed
