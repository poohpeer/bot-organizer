"""Writing a place into the session's `places` table.

Separate from bot/tools/composed.py, which owns the model-facing tools:
bot/router.py records a pin somebody dropped in the chat, and that is not a
tool call — importing composed for one INSERT would drag the whole external
API surface into the router.
"""


async def save_shared_location(pool, session_id: int, *, name, address, lat, lon) -> None:
    """Store a location shared in the chat, replacing the coordinates of a
    place of the same name.

    DO UPDATE, not DO NOTHING. A group that pins the same place again is
    correcting it — the earlier pin was on the wrong side of the park, or
    resolve_and_save_place guessed the venue from a name and a human is now
    saying where it actually is. `resolved_at` moves too, so the corrected
    row is the one _current_place falls back to.

    The address is only overwritten when the new one says something: a bare
    pin carries none, and letting it blank out the address a venue or a maps
    lookup already supplied would lose information for no reason.
    """
    await pool.execute(
        """
        INSERT INTO places (session_id, name, address, lat, lon, query)
        VALUES ($1, $2, $3, $4, $5, $2)
        ON CONFLICT (session_id, lower(name)) DO UPDATE SET
            lat = EXCLUDED.lat,
            lon = EXCLUDED.lon,
            address = COALESCE(EXCLUDED.address, places.address),
            resolved_at = now()
        """,
        session_id, name, address, lat, lon,
    )
