import functools
from datetime import date

from bot.tools.external import maps_lookup

_HALF_LIFE_DAYS = 180


async def resolve_and_save_place(pool, session_id, place_query) -> dict:
    cached = await pool.fetchrow(
        "SELECT name, address, lat, lon FROM places WHERE session_id = $1 AND lower(name) = lower($2)",
        session_id, place_query,
    )
    if cached:
        return {
            "found": True, "name": cached["name"], "address": cached["address"],
            "lat": cached["lat"], "lon": cached["lon"], "cached": True,
        }

    looked_up = await maps_lookup(place_query)
    if not looked_up.get("found"):
        return {"found": False}

    await pool.execute(
        "INSERT INTO places (session_id, name, address, lat, lon) VALUES ($1, $2, $3, $4, $5)",
        session_id, looked_up["name"], looked_up["address"], looked_up["lat"], looked_up["lon"],
    )
    return {
        "found": True, "name": looked_up["name"], "address": looked_up["address"],
        "lat": looked_up["lat"], "lon": looked_up["lon"], "cached": False,
    }


async def send_location(pool, telegram_bot, session_id, chat_id, place_name) -> dict:
    row = await pool.fetchrow(
        "SELECT name, address, lat, lon FROM places WHERE session_id = $1 AND lower(name) = lower($2)",
        session_id, place_name,
    )
    if row is None:
        return {"status": "not_found"}

    await telegram_bot.send_venue(
        chat_id=chat_id, latitude=row["lat"], longitude=row["lon"],
        title=row["name"], address=row["address"] or "",
    )
    return {"status": "ok"}


async def archive_lookup(pool, chat_id, activity_type) -> dict:
    rows = await pool.fetch(
        """
        SELECT pl.name, COALESCE(s.event_date, s.closed_at::date) AS visited_on
        FROM places pl
        JOIN sessions s ON s.id = pl.session_id
        WHERE s.chat_id = $1 AND s.activity_type = $2 AND s.status = 'closed'
        """,
        chat_id, activity_type,
    )
    if not rows:
        return {"pattern": "no_history", "places": []}

    today = date.today()
    visits_by_name: dict[str, list[date]] = {}
    for r in rows:
        visits_by_name.setdefault(r["name"], []).append(r["visited_on"])

    places = []
    for name, visits in visits_by_name.items():
        score = sum(0.5 ** ((today - v).days / _HALF_LIFE_DAYS) for v in visits)
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
    if len(places) == 1 or top["score"] >= 2 * places[1]["score"]:
        return {"pattern": "dominant", "places": [top]}

    tied = [p for p in places if p["score"] >= top["score"] * 0.8]
    return {"pattern": "tied", "places": tied}


def build_composed_registry(pool, telegram_bot) -> dict:
    return {
        "resolve_and_save_place": functools.partial(resolve_and_save_place, pool),
        "send_location": functools.partial(send_location, pool, telegram_bot),
        "archive_lookup": functools.partial(archive_lookup, pool),
    }
