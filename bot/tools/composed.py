from bot.tools.external import maps_lookup


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
