from unittest.mock import AsyncMock, patch

import bot.tools.composed as composed


async def _new_session(db_pool, chat_id=1, activity_type="picnic", status="active"):
    await db_pool.execute(
        "INSERT INTO chats (chat_id, title) VALUES ($1, 'Chat') ON CONFLICT DO NOTHING", chat_id
    )
    row = await db_pool.fetchrow(
        "INSERT INTO sessions (chat_id, activity_type, status) VALUES ($1, $2, $3) RETURNING id",
        chat_id, activity_type, status,
    )
    return row["id"]


async def test_resolve_and_save_place_queries_maps_when_uncached(db_pool):
    session_id = await _new_session(db_pool)
    maps_result = {
        "found": True, "name": "Hanania Meadow", "address": "Route 1",
        "lat": 32.79, "lon": 35.05, "rating": 4.2, "review_snippets": [],
    }

    with patch("bot.tools.composed.maps_lookup", AsyncMock(return_value=maps_result)) as mocked:
        result = await composed.resolve_and_save_place(db_pool, session_id, "Hanania meadow")

    mocked.assert_awaited_once_with("Hanania meadow")
    assert result == {
        "found": True, "name": "Hanania Meadow", "address": "Route 1",
        "lat": 32.79, "lon": 35.05, "cached": False,
    }
    saved = await db_pool.fetchrow("SELECT * FROM places WHERE session_id = $1", session_id)
    assert saved["name"] == "Hanania Meadow"


async def test_resolve_and_save_place_reuses_cached_match(db_pool):
    session_id = await _new_session(db_pool)
    await db_pool.execute(
        "INSERT INTO places (session_id, name, address, lat, lon) VALUES ($1, 'Hanania Meadow', 'Route 1', 32.79, 35.05)",
        session_id,
    )

    with patch("bot.tools.composed.maps_lookup", AsyncMock()) as mocked:
        result = await composed.resolve_and_save_place(db_pool, session_id, "hanania meadow")

    mocked.assert_not_awaited()
    assert result == {
        "found": True, "name": "Hanania Meadow", "address": "Route 1",
        "lat": 32.79, "lon": 35.05, "cached": True,
    }


async def test_resolve_and_save_place_not_found(db_pool):
    session_id = await _new_session(db_pool)

    with patch("bot.tools.composed.maps_lookup", AsyncMock(return_value={"found": False})):
        result = await composed.resolve_and_save_place(db_pool, session_id, "nowhere in particular")

    assert result == {"found": False}
    assert await db_pool.fetchrow("SELECT * FROM places WHERE session_id = $1", session_id) is None
