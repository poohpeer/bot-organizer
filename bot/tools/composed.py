import functools
from datetime import date

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

    facts = (await core_tools.get_facts(pool, session_id, key="place"))["facts"]
    participants = await core_tools.get_participants(pool, session_id)
    listing = await core_tools.list_show(pool, session_id)
    reminders = await core_tools.reminder_list(pool, session_id)

    event_date = session_row["event_date"].strftime("%d/%m") if session_row["event_date"] else None
    report = status_render.render_status(
        place=facts.get("place"),
        event_date=event_date,
        participants_rendered=participants["rendered"],
        list_rendered=listing["rendered"],
        reminders_rendered=status_render.render_reminders(reminders["reminders"]),
    )
    return {"status": "ok", "report": report}


def build_composed_registry(pool, telegram_bot) -> dict:
    return {
        "resolve_and_save_place": functools.partial(resolve_and_save_place, pool),
        "send_location": functools.partial(send_location, pool, telegram_bot),
        "archive_lookup": functools.partial(archive_lookup, pool),
        "event_status": functools.partial(event_status, pool),
    }
