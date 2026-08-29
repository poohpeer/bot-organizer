import functools
from datetime import date

import bot.group_info as group_info
import bot.maps_links as maps_links
import bot.timezones as timezones
import bot.tools.core as core_tools
import bot.status_render as status_render
from bot.tools.external import maps_lookup

_HALF_LIFE_DAYS = 180

# A place must score at least this multiple of the runner-up to be called the
# group's usual spot. Below it, the runner-up is close enough that picking one
# would be arbitrary — so the same boundary decides which places are shown as
# tied options (R8). Using two different thresholds would leave a dead band
# where the answer is "tied" but only one option comes back.
_DOMINANCE_RATIO = 2.0

# Matches a saved place by the canonical name maps returned *or* by the phrasing
# that originally resolved it, so the group can keep calling it whatever they
# call it.
_PLACE_MATCH = """
    SELECT name, address, lat, lon FROM places
    WHERE session_id = $1 AND (lower(name) = lower($2) OR lower(query) = lower($2))
    ORDER BY resolved_at LIMIT 1
"""


async def _session_row(pool, session_id, columns="chat_id"):
    return await pool.fetchrow(f"SELECT {columns} FROM sessions WHERE id = $1", session_id)


async def resolve_and_save_place(pool, session_id, place_query) -> dict:
    session_row = await _session_row(pool, session_id)
    if session_row is None:
        return {"found": False, "status": "unknown_session"}

    cached = await pool.fetchrow(_PLACE_MATCH, session_id, place_query)
    if cached:
        return {
            "found": True, "name": cached["name"], "address": cached["address"],
            "lat": cached["lat"], "lon": cached["lon"], "cached": True,
        }

    looked_up = await maps_lookup(place_query)
    if not looked_up.get("found"):
        return {"found": False}

    # A concurrent resolve of the same place may have inserted it already; the
    # unique index makes that a no-op rather than a duplicate visit in the
    # archive.
    await pool.execute(
        """
        INSERT INTO places (session_id, name, address, lat, lon, query)
        VALUES ($1, $2, $3, $4, $5, $6)
        ON CONFLICT (session_id, lower(name)) DO NOTHING
        """,
        session_id, looked_up["name"], looked_up["address"],
        looked_up["lat"], looked_up["lon"], place_query,
    )
    # A resolved place is the one moment we can learn the group's timezone for
    # free, so reminders land at the right local time. Done here in code rather
    # than left to weather_lookup, which only runs when the model decides the
    # weather is relevant — neither guaranteed nor predictable.
    await timezones.learn_timezone_from_coordinates(
        pool, session_row["chat_id"], looked_up["lat"], looked_up["lon"]
    )
    return {
        "found": True, "name": looked_up["name"], "address": looked_up["address"],
        "lat": looked_up["lat"], "lon": looked_up["lon"], "cached": False,
    }


async def send_location(pool, telegram_bot, session_id, place_name) -> dict:
    """Send a saved place as a native Telegram venue card.

    chat_id is derived from the session, never taken from the model: a
    hallucinated chat_id would post this group's plans into another group.
    """
    session_row = await _session_row(pool, session_id)
    if session_row is None:
        return {"status": "unknown_session"}

    row = await pool.fetchrow(_PLACE_MATCH, session_id, place_name)
    if row is None:
        return {"status": "not_found"}

    await telegram_bot.send_venue(
        chat_id=session_row["chat_id"], latitude=row["lat"], longitude=row["lon"],
        title=row["name"], address=row["address"] or "",
    )
    return {"status": "ok"}


async def archive_lookup(pool, session_id, activity_type) -> dict:
    """Rank the places this chat has used before for an activity, weighting
    recent visits more heavily than old ones (R8).

    Scoped by the session's own chat_id rather than a model-supplied one, so a
    hallucinated id can't surface another group's history.
    """
    session_row = await _session_row(pool, session_id)
    if session_row is None:
        return {"pattern": "unknown_session", "places": []}

    rows = await pool.fetch(
        """
        SELECT pl.name, COALESCE(s.event_date, s.closed_at::date, s.started_at::date) AS visited_on
        FROM places pl
        JOIN sessions s ON s.id = pl.session_id
        WHERE s.chat_id = $1 AND s.activity_type = $2 AND s.status = 'closed'
        """,
        session_row["chat_id"], activity_type,
    )
    if not rows:
        return {"pattern": "no_history", "places": []}

    today = date.today()
    visits_by_name: dict[str, list[date]] = {}
    for r in rows:
        visits_by_name.setdefault(r["name"], []).append(r["visited_on"])

    places = []
    for name, visits in visits_by_name.items():
        # Clamped at 0: a session closed while its event_date is still in the
        # future never happened, and an unclamped negative age would score it
        # above 1.0 per visit — letting an abandoned plan outrank the place the
        # group actually goes to.
        score = sum(0.5 ** (max((today - v).days, 0) / _HALF_LIFE_DAYS) for v in visits)
        places.append({
            "name": name,
            "visit_count": len(visits),
            "last_visited": max(visits).isoformat(),
            "score": round(score, 4),
        })
    places.sort(key=lambda p: p["score"], reverse=True)

    if all(p["visit_count"] == 1 for p in places):
        return {"pattern": "no_pattern", "places": places}

    top = places[0]
    if len(places) == 1 or top["score"] >= _DOMINANCE_RATIO * places[1]["score"]:
        return {"pattern": "dominant", "places": [top]}

    tied = [p for p in places if p["score"] * _DOMINANCE_RATIO >= top["score"]]
    return {"pattern": "tied", "places": tied}


async def current_place(pool, session_id) -> dict:
    """Where the event is and, when known, its coordinates.

    Two stores hold this and they disagree in practice. `places` is the
    canonical one — resolve_and_save_place writes a maps-resolved name and
    address there — while the facts table holds whatever the model chose to
    write. event_status used to read only the fact, under the exact key
    "place", and in one real session nothing ever wrote that key: the model
    picked "destination", then "event_name", so the 📍 line stayed blank for
    the session's whole life while `places` held two resolved addresses.

    The fact wins when present, because someone stating a place in
    conversation is more current than an older lookup; `places` is the
    fallback, most recently resolved first.

    Either way this reports the place *as the group wrote it*, never the name
    maps came back with. Those differ, and not subtly: a chat called
    "Море 20/11" had its place resolved to "The Old Man and the Sea", a
    restaurant, and the status report announced that as the meeting place.
    The lookup is worth keeping for coordinates and the venue card
    (send_location uses the canonical name there, which is right for a map
    pin), but the report is meant to repeat the group back to itself.

    The coordinates are what the report turns into a map link, so they are
    looked up *for the name being reported* rather than taken from whatever
    was resolved most recently — a pin someone dropped for a shop must not
    end up linked beside the picnic's own place.
    """
    fact = (await core_tools.get_facts(pool, session_id, key="place"))["facts"].get("place")
    latest = await pool.fetchrow(
        "SELECT query, name, lat, lon FROM places WHERE session_id = $1 "
        "ORDER BY resolved_at DESC LIMIT 1",
        session_id,
    )

    if fact:
        # Coordinates for the name the group uses, not for whatever was
        # resolved last: a pin for a shop someone mentioned must not put its
        # link beside the picnic's own place. _PLACE_MATCH matches the
        # canonical name or the phrasing that resolved it, which is how a
        # group's "Море" finds the row it saved.
        row = await pool.fetchrow(_PLACE_MATCH, session_id, fact)
        return {"name": fact,
                "lat": row["lat"] if row else None,
                "lon": row["lon"] if row else None}

    if latest is None:
        return {"name": None, "lat": None, "lon": None}
    # query is the phrasing that resolved it — what someone actually typed.
    # It is nullable for rows written before it was recorded, so name is the
    # last resort rather than the default.
    return {"name": latest["query"] or latest["name"],
            "lat": latest["lat"], "lon": latest["lon"]}


async def event_status(pool, session_id) -> dict:
    """The whole "как дела с организацией" report, as one fixed block of
    text (report), built once here rather than reassembled by the model on
    every reply. Requested live after the model's own free-composed version
    drifted from the desired format across several replies — headers
    missing, place and date folded together, no reminders section at all.
    """
    session_row = await _session_row(pool, session_id, columns="event_date")
    if session_row is None:
        return {"status": "unknown_session"}

    place = await current_place(pool, session_id)
    participants = await core_tools.get_participants(pool, session_id)
    listing = await core_tools.list_show(pool, session_id)
    reminders = await core_tools.reminder_list(pool, session_id)

    event_date = session_row["event_date"].strftime("%d/%m") if session_row["event_date"] else None
    report = status_render.render_status(
        place=place["name"],
        place_link=maps_links.maps_link(place["lat"], place["lon"]),
        event_date=event_date,
        participants_rendered=participants["rendered"],
        list_rendered=listing["rendered"],
        reminders_rendered=status_render.render_reminders(reminders["reminders"]),
    )
    return {"status": "ok", "report": report}


async def get_chat_info(pool, telegram_bot, session_id) -> dict:
    """The group's own current title and description, fetched live.

    chats.title is written once, only while the chat is still dormant
    (bot.router.handle_dormant_message), and never refreshed once a session
    goes active — so it goes stale exactly when a real answer is needed most.
    chats.title_seen is fresher (worker.group_sync keeps it current for any
    chat with an active session) but exists only for change-detection, not
    for a question asked on demand right now. A live get_chat is cheap and
    guarantees the answer is never older than this call.

    Read-only, deliberately: this has no side effects, so it is safe to call
    for any question about the chat's name or description without risking a
    write. Recording what is found is sync_chat_info's job, not this one.
    """
    session_row = await _session_row(pool, session_id)
    if session_row is None:
        return {"status": "unknown_session"}

    info = await group_info.fetch(telegram_bot, session_row["chat_id"])
    if not info:
        return {"status": "unavailable"}
    return {"status": "ok", "title": info.get("title"), "description": info.get("description")}


async def sync_chat_info(pool, telegram_bot, session_id) -> dict:
    """Read the group's title/description live and actually record whatever
    event info they state — place and/or date — in one atomic call.

    Requested live after the model replied "Записал место встречи: Море" to
    a "посмотри в названии группы место встречи и запиши" request without any
    remember_fact call actually happening: a follow-up "покажи статус" showed
    no place at all. Chaining "read via get_chat_info, then decide to also
    call remember_fact" left the write dependent on the primary model
    reliably making a second tool call in the same turn, which it did not.
    Doing both in one call removes that dependency the same way event_status
    replaced free-form composition of several tools with one fixed result.

    Only called for an explicit "record/save what the title says" request —
    unlike worker.group_sync, which only overwrites on a detected title
    change, this runs whenever asked and would otherwise silently clobber a
    place the group already confirmed by conversation with stale text from
    an unchanged title. That trade-off is fine here: the caller explicitly
    asked to sync from the title just now.
    """
    session_row = await _session_row(pool, session_id, columns="chat_id, event_date")
    if session_row is None:
        return {"status": "unknown_session"}

    info = await group_info.fetch(telegram_bot, session_row["chat_id"])
    if not info:
        return {"status": "unavailable"}

    tz = await timezones.chat_timezone(pool, session_row["chat_id"])
    event = await group_info.extract_event(
        info.get("title"), info.get("description"), timezones.local_date(tz)
    )

    if not event["answered"]:
        # Nobody in the model chain could read the title. Saying "в названии
        # ничего нет" here would be a lie about the group's own text.
        return {"status": "unavailable",
                "title": info.get("title"), "description": info.get("description")}

    # Same rules as the timer pass: a place only when the text names somewhere
    # the group could actually go, and a write only when the value differs
    # from what the session already holds.
    changed = await group_info.apply_event(pool, session_id, event)

    # found_nothing is about the text, not about the write. An unchanged
    # value was still found: answering "в названии ничего нет" because the
    # place was already recorded would tell the group the opposite of what
    # their own title says.
    found = {key: event[key] for key in ("event_date", "place") if event.get(key)}
    return {
        "status": "ok",
        "title": info.get("title"),
        "description": info.get("description"),
        "found_nothing": not found,
        "already_current": bool(found) and not changed,
        **found,
        "changed": changed,
    }


def build_composed_registry(pool, telegram_bot) -> dict:
    return {
        "resolve_and_save_place": functools.partial(resolve_and_save_place, pool),
        "send_location": functools.partial(send_location, pool, telegram_bot),
        "archive_lookup": functools.partial(archive_lookup, pool),
        "event_status": functools.partial(event_status, pool),
        "get_chat_info": functools.partial(get_chat_info, pool, telegram_bot),
        "sync_chat_info": functools.partial(sync_chat_info, pool, telegram_bot),
    }
