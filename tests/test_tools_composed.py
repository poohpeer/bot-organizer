import datetime as dt
import inspect
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import bot.tools.composed as composed
import bot.tools.schema as schema

_MAPS_RESULT = {
    "found": True, "name": "Hanania Meadow", "address": "Route 1",
    "lat": 32.79, "lon": 35.05, "rating": 4.2, "review_snippets": [],
}


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

    with patch("bot.tools.composed.maps_lookup", AsyncMock(return_value=_MAPS_RESULT)) as mocked:
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


async def test_resolve_and_save_place_caches_by_the_phrasing_that_resolved_it(db_pool):
    """R9: the group calls it "поляна Ханания"; maps calls it "Hanania Meadow".
    Matching only the canonical name would re-query maps and duplicate the row
    every single time they use their own name for it."""
    session_id = await _new_session(db_pool)

    with patch("bot.tools.composed.maps_lookup", AsyncMock(return_value=_MAPS_RESULT)) as mocked:
        await composed.resolve_and_save_place(db_pool, session_id, "поляна Ханания")
        second = await composed.resolve_and_save_place(db_pool, session_id, "поляна Ханания")

    assert mocked.await_count == 1
    assert second["cached"] is True
    rows = await db_pool.fetchval("SELECT count(*) FROM places WHERE session_id = $1", session_id)
    assert rows == 1


async def test_resolve_and_save_place_does_not_duplicate_an_already_saved_place(db_pool):
    """Two different phrasings resolving to the same place stay one row —
    otherwise archive_lookup counts one outing as several visits."""
    session_id = await _new_session(db_pool)

    with patch("bot.tools.composed.maps_lookup", AsyncMock(return_value=_MAPS_RESULT)):
        await composed.resolve_and_save_place(db_pool, session_id, "поляна Ханания")
        await composed.resolve_and_save_place(db_pool, session_id, "тот луг за городом")

    rows = await db_pool.fetchval("SELECT count(*) FROM places WHERE session_id = $1", session_id)
    assert rows == 1


async def test_resolve_and_save_place_not_found(db_pool):
    session_id = await _new_session(db_pool)

    with patch("bot.tools.composed.maps_lookup", AsyncMock(return_value={"found": False})):
        result = await composed.resolve_and_save_place(db_pool, session_id, "nowhere in particular")

    assert result == {"found": False}
    assert await db_pool.fetchrow("SELECT * FROM places WHERE session_id = $1", session_id) is None


async def test_send_location_sends_venue_for_saved_place(db_pool):
    session_id = await _new_session(db_pool)
    await db_pool.execute(
        "INSERT INTO places (session_id, name, address, lat, lon) VALUES ($1, 'Hanania Meadow', 'Route 1', 32.79, 35.05)",
        session_id,
    )
    telegram_bot = AsyncMock()

    result = await composed.send_location(db_pool, telegram_bot, session_id, place_name="hanania meadow")

    assert result == {"status": "ok"}
    telegram_bot.send_venue.assert_awaited_once_with(
        chat_id=1, latitude=32.79, longitude=35.05, title="Hanania Meadow", address="Route 1",
    )


async def test_send_location_posts_to_the_sessions_own_chat(db_pool):
    """chat_id is derived from the session, not supplied by the model, so a
    hallucinated id cannot leak one group's plans into another's chat."""
    session_id = await _new_session(db_pool, chat_id=777)
    await db_pool.execute(
        "INSERT INTO places (session_id, name, address, lat, lon) VALUES ($1, 'Hanania Meadow', 'Route 1', 32.79, 35.05)",
        session_id,
    )
    telegram_bot = AsyncMock()

    await composed.send_location(db_pool, telegram_bot, session_id, place_name="Hanania Meadow")

    assert telegram_bot.send_venue.await_args.kwargs["chat_id"] == 777


async def test_send_location_finds_the_place_by_the_original_phrasing(db_pool):
    session_id = await _new_session(db_pool)
    with patch("bot.tools.composed.maps_lookup", AsyncMock(return_value=_MAPS_RESULT)):
        await composed.resolve_and_save_place(db_pool, session_id, "поляна Ханания")
    telegram_bot = AsyncMock()

    result = await composed.send_location(db_pool, telegram_bot, session_id, place_name="поляна Ханания")

    assert result == {"status": "ok"}
    telegram_bot.send_venue.assert_awaited_once()


async def test_send_location_not_found_for_unresolved_place(db_pool):
    session_id = await _new_session(db_pool)
    telegram_bot = AsyncMock()

    result = await composed.send_location(db_pool, telegram_bot, session_id, place_name="nowhere")

    assert result == {"status": "not_found"}
    telegram_bot.send_venue.assert_not_awaited()


async def test_send_location_unknown_session(db_pool):
    telegram_bot = AsyncMock()

    result = await composed.send_location(db_pool, telegram_bot, 999999, place_name="anywhere")

    assert result == {"status": "unknown_session"}
    telegram_bot.send_venue.assert_not_awaited()


async def _closed_session_with_place(db_pool, chat_id, activity_type, place_name, days_ago):
    await db_pool.execute(
        "INSERT INTO chats (chat_id, title) VALUES ($1, 'Chat') ON CONFLICT DO NOTHING", chat_id
    )
    visited_on = dt.date.today() - dt.timedelta(days=days_ago)
    row = await db_pool.fetchrow(
        """
        INSERT INTO sessions (chat_id, activity_type, status, event_date, closed_at)
        VALUES ($1, $2, 'closed', $3, now()) RETURNING id
        """,
        chat_id, activity_type, visited_on,
    )
    await db_pool.execute(
        "INSERT INTO places (session_id, name, lat, lon) VALUES ($1, $2, 0, 0)",
        row["id"], place_name,
    )


async def test_archive_lookup_no_history(db_pool):
    session_id = await _new_session(db_pool)

    result = await composed.archive_lookup(db_pool, session_id, activity_type="picnic")

    assert result == {"pattern": "no_history", "places": []}


async def test_archive_lookup_dominant_place_wins_on_recency(db_pool):
    # Visited 5 times but stale (2 years ago) vs 1 recent visit — recency wins.
    for _ in range(5):
        await _closed_session_with_place(db_pool, 1, "picnic", "Old Spot", days_ago=730)
    await _closed_session_with_place(db_pool, 1, "picnic", "Fresh Spot", days_ago=10)
    session_id = await _new_session(db_pool)

    result = await composed.archive_lookup(db_pool, session_id, activity_type="picnic")

    assert result["pattern"] == "dominant"
    assert result["places"][0]["name"] == "Fresh Spot"


async def test_archive_lookup_tied_shows_top_options(db_pool):
    await _closed_session_with_place(db_pool, 1, "picnic", "Spot A", days_ago=10)
    await _closed_session_with_place(db_pool, 1, "picnic", "Spot A", days_ago=20)
    await _closed_session_with_place(db_pool, 1, "picnic", "Spot B", days_ago=12)
    await _closed_session_with_place(db_pool, 1, "picnic", "Spot B", days_ago=22)
    session_id = await _new_session(db_pool)

    result = await composed.archive_lookup(db_pool, session_id, activity_type="picnic")

    assert result["pattern"] == "tied"
    assert {p["name"] for p in result["places"]} == {"Spot A", "Spot B"}


async def test_archive_lookup_tied_never_returns_a_single_option(db_pool):
    """R8 asks the bot to list the options and ask which one. A score ratio
    between the dominance cut and the old 0.8 tie cut used to report "tied"
    while handing back exactly one place — nothing to choose between."""
    await _closed_session_with_place(db_pool, 1, "picnic", "Spot A", days_ago=5)
    await _closed_session_with_place(db_pool, 1, "picnic", "Spot A", days_ago=15)
    await _closed_session_with_place(db_pool, 1, "picnic", "Spot B", days_ago=120)
    await _closed_session_with_place(db_pool, 1, "picnic", "Spot B", days_ago=130)
    session_id = await _new_session(db_pool)

    result = await composed.archive_lookup(db_pool, session_id, activity_type="picnic")

    assert result["pattern"] == "tied"
    assert len(result["places"]) > 1


async def test_archive_lookup_no_pattern_when_every_place_visited_once(db_pool):
    await _closed_session_with_place(db_pool, 1, "picnic", "Spot A", days_ago=100)
    await _closed_session_with_place(db_pool, 1, "picnic", "Spot B", days_ago=200)
    await _closed_session_with_place(db_pool, 1, "picnic", "Spot C", days_ago=300)
    session_id = await _new_session(db_pool)

    result = await composed.archive_lookup(db_pool, session_id, activity_type="picnic")

    assert result["pattern"] == "no_pattern"
    assert len(result["places"]) == 3


async def test_archive_lookup_ignores_other_activity_types_and_open_sessions(db_pool):
    await _closed_session_with_place(db_pool, 1, "birthday", "Wrong Activity", days_ago=1)
    session_id = await _new_session(db_pool, chat_id=1, activity_type="picnic", status="active")
    await db_pool.execute(
        "INSERT INTO places (session_id, name, lat, lon) VALUES ($1, 'Still Open', 0, 0)", session_id
    )

    result = await composed.archive_lookup(db_pool, session_id, activity_type="picnic")

    assert result == {"pattern": "no_history", "places": []}


async def test_archive_lookup_only_sees_its_own_chats_history(db_pool):
    """The archive is scoped by the session's chat_id, not a model-supplied
    one — one group's history must never surface in another group."""
    await _closed_session_with_place(db_pool, 1, "picnic", "Chat One Spot", days_ago=10)
    await _closed_session_with_place(db_pool, 1, "picnic", "Chat One Spot", days_ago=20)
    other_session = await _new_session(db_pool, chat_id=2)

    result = await composed.archive_lookup(db_pool, other_session, activity_type="picnic")

    assert result == {"pattern": "no_history", "places": []}


async def test_archive_lookup_survives_a_session_closed_without_any_date(db_pool):
    """event_date and closed_at can both be absent; started_at never is. Without
    that final fallback the scoring loop raised TypeError on None."""
    session_id = await _new_session(db_pool, chat_id=1, activity_type="picnic")
    await db_pool.execute(
        "UPDATE sessions SET status = 'closed', closed_at = NULL WHERE id = $1", session_id
    )
    await db_pool.execute(
        "INSERT INTO places (session_id, name, lat, lon) VALUES ($1, 'Undated Spot', 0, 0)", session_id
    )
    active = await _new_session(db_pool, chat_id=1)

    result = await composed.archive_lookup(db_pool, active, activity_type="picnic")

    assert result["places"][0]["name"] == "Undated Spot"


async def test_archive_lookup_does_not_let_an_abandoned_future_plan_win(db_pool):
    """A session closed while its event_date is still ahead never happened. An
    unclamped negative age scored it above a same-day visit, so one abandoned
    plan outranked the place the group actually keeps going to."""
    await _closed_session_with_place(db_pool, 1, "picnic", "Never Happened", days_ago=-365)
    for days in (5, 15, 25):
        await _closed_session_with_place(db_pool, 1, "picnic", "Real Favourite", days_ago=days)
    session_id = await _new_session(db_pool)

    result = await composed.archive_lookup(db_pool, session_id, activity_type="picnic")

    assert result["places"][0]["name"] == "Real Favourite"


async def test_archive_lookup_unknown_session(db_pool):
    result = await composed.archive_lookup(db_pool, 999999, activity_type="picnic")

    assert result == {"pattern": "unknown_session", "places": []}


def test_build_composed_registry_covers_every_composed_tool(db_pool):
    registry = composed.build_composed_registry(db_pool, AsyncMock())

    assert set(registry) == {
        "resolve_and_save_place", "send_location", "archive_lookup", "event_status",
        "get_chat_info", "sync_chat_info",
    }


def test_declared_parameters_match_the_bound_signatures(db_pool):
    """The model can only pass what the declaration advertises, so a drifted
    declaration is either a TypeError at call time or — the case that bit us —
    a chat_id the model gets to choose."""
    registry = composed.build_composed_registry(db_pool, AsyncMock())
    declared = {
        fn.name: set(fn.parameters.properties)
        for fn in schema.ALL_TOOLS.function_declarations
        if fn.name in registry
    }

    for name, tool in registry.items():
        bound = set(inspect.signature(tool).parameters)
        assert bound == declared[name], f"{name}: declared {declared[name]}, accepts {bound}"


async def test_event_status_matches_the_exact_reported_scenario(db_pool):
    """End to end against a real database, reproducing the live scenario that
    prompted this tool: the model's own free-composed report had missing
    emoji, no reminders section, and place/date folded into one line."""
    import bot.tools.core as core_tools

    session_id = await db_pool.fetchval(
        "INSERT INTO chats (chat_id, title) VALUES (1, 'Chat') RETURNING chat_id"
    ) and await db_pool.fetchval(
        "INSERT INTO sessions (chat_id, activity_type, event_date) VALUES (1, 'picnic', '2026-11-20') RETURNING id"
    )
    await core_tools.remember_fact(db_pool, session_id, "place", "Tel Aviv, sea")
    await core_tools.set_participant(db_pool, session_id, "Витька", "unknown", user_id=2)
    await core_tools.set_participant(db_pool, session_id, "Андрюха", "confirmed", user_id=1)
    await core_tools.set_participant(db_pool, session_id, "Alex", "unknown", user_id=3)
    await core_tools.list_add(db_pool, session_id, "пиво")
    await core_tools.list_add(db_pool, session_id, "арбуз")
    await core_tools.list_claim(db_pool, session_id, "арбуз", claimed_by="Alex")

    result = await composed.event_status(db_pool, session_id)

    assert result["status"] == "ok"
    assert result["report"] == (
        "Вот текущая информация по организации встречи:\n\n"
        "📍 Место: Tel Aviv, sea\n"
        "📅 Дата: 20/11\n\n"
        "👥 Участники:\n"
        "◻️ Витька\n✅ Андрюха\n◻️ Alex\n\n"
        "🛒 Список покупок / вещей:\n\n"
        "Ещё не разобрали:\n◻️ пиво\n\n"
        "Уже взяли:\n✅ арбуз — Alex\n\n"
        "⏰ Напоминания:\n"
        "Нет запланированных напоминаний"
    )


async def test_event_status_unknown_session(db_pool):
    result = await composed.event_status(db_pool, 999999)

    assert result == {"status": "unknown_session"}


async def test_event_status_with_nothing_recorded_yet(db_pool):
    session_id = await _new_session(db_pool)

    result = await composed.event_status(db_pool, session_id)

    assert result["status"] == "ok"
    assert "📍" not in result["report"]
    assert "Пока никого не записал." in result["report"]
    assert "Список пока пуст." in result["report"]
    assert "Нет запланированных напоминаний" in result["report"]


async def test_get_chat_info_returns_the_live_title_and_description(db_pool):
    """Reproduces the live bug: chats.title is written once while a chat is
    still dormant and never refreshed once a session goes active, so a
    question asked mid-conversation must go straight to Telegram rather than
    read the (possibly stale) stored column."""
    session_id = await _new_session(db_pool, chat_id=42)
    telegram_bot = AsyncMock()
    telegram_bot.get_chat.return_value = SimpleNamespace(
        title="Пикник в субботу", description="15 сентября, поляна Ханания"
    )
    telegram_bot.get_chat_member_count.return_value = 9

    result = await composed.get_chat_info(db_pool, telegram_bot, session_id)

    assert result == {
        "status": "ok", "title": "Пикник в субботу", "description": "15 сентября, поляна Ханания",
    }
    telegram_bot.get_chat.assert_awaited_once_with(42)


async def test_get_chat_info_reflects_a_title_changed_after_the_session_started(db_pool):
    """The stale-column bug this tool exists to fix: chats.title is set once
    from the dormant-chat greeting path and never touched again, so a rename
    after that point must still be visible on demand."""
    session_id = await _new_session(db_pool, chat_id=1)
    telegram_bot = AsyncMock()
    telegram_bot.get_chat.return_value = SimpleNamespace(title="Новое название", description=None)
    telegram_bot.get_chat_member_count.return_value = 3

    result = await composed.get_chat_info(db_pool, telegram_bot, session_id)

    assert result["status"] == "ok"
    assert result["title"] == "Новое название"
    # The stored chats.title from _new_session's insert ('Chat') is untouched
    # — proof this reads live rather than from the stale column.
    stored = await db_pool.fetchval("SELECT title FROM chats WHERE chat_id = 1")
    assert stored == "Chat"


async def test_get_chat_info_unknown_session(db_pool):
    telegram_bot = AsyncMock()

    result = await composed.get_chat_info(db_pool, telegram_bot, 999999)

    assert result == {"status": "unknown_session"}
    telegram_bot.get_chat.assert_not_awaited()


async def test_get_chat_info_unavailable_when_telegram_call_fails(db_pool):
    """Mirrors group_info.fetch's own contract: never raise, and never invent
    an answer when the chat is unreachable (kicked, banned, network blip)."""
    session_id = await _new_session(db_pool, chat_id=7)
    telegram_bot = AsyncMock()
    telegram_bot.get_chat.side_effect = RuntimeError("Forbidden: bot was kicked")

    result = await composed.get_chat_info(db_pool, telegram_bot, session_id)

    assert result == {"status": "unavailable"}


async def test_sync_chat_info_saves_place_and_date_from_the_title(db_pool, monkeypatch):
    """Reproduces the live bug: the model replied "Записал место встречи:
    Море" after get_chat_info, but nothing was actually recorded — a later
    "покажи статус" showed no place at all. sync_chat_info must save the
    place/date in the same call that reads them, not depend on the primary
    model chaining a second tool call correctly."""
    import bot.tools.core as core_tools

    session_id = await _new_session(db_pool, chat_id=42)
    telegram_bot = AsyncMock()
    telegram_bot.get_chat.return_value = SimpleNamespace(
        title="Поездка на море", description="20 ноября"
    )
    telegram_bot.get_chat_member_count.return_value = 4
    monkeypatch.setattr(
        composed.group_info, "extract",
        AsyncMock(return_value={"activity_type": "поездка", "event_date": "2026-11-20", "place": "Море"}),
    )

    result = await composed.sync_chat_info(db_pool, telegram_bot, session_id)

    assert result["status"] == "ok"
    assert result["found_nothing"] is False
    assert result["place"] == "Море"
    assert result["event_date"] == "2026-11-20"

    facts = (await core_tools.get_facts(db_pool, session_id, key="place"))["facts"]
    assert facts["place"] == "Море"
    stored_date = await db_pool.fetchval("SELECT event_date FROM sessions WHERE id = $1", session_id)
    assert stored_date.isoformat() == "2026-11-20"


async def test_sync_chat_info_found_nothing_saves_nothing(db_pool, monkeypatch):
    session_id = await _new_session(db_pool, chat_id=1)
    telegram_bot = AsyncMock()
    telegram_bot.get_chat.return_value = SimpleNamespace(title="Общий чат", description=None)
    telegram_bot.get_chat_member_count.return_value = 4
    monkeypatch.setattr(
        composed.group_info, "extract",
        AsyncMock(return_value={"activity_type": None, "event_date": None, "place": None}),
    )

    result = await composed.sync_chat_info(db_pool, telegram_bot, session_id)

    assert result["status"] == "ok"
    assert result["found_nothing"] is True
    assert "place" not in result
    assert "event_date" not in result

    stored_date = await db_pool.fetchval("SELECT event_date FROM sessions WHERE id = $1", session_id)
    assert stored_date is None


async def test_sync_chat_info_unknown_session(db_pool):
    telegram_bot = AsyncMock()

    result = await composed.sync_chat_info(db_pool, telegram_bot, 999999)

    assert result == {"status": "unknown_session"}
    telegram_bot.get_chat.assert_not_awaited()


async def test_sync_chat_info_unavailable_when_telegram_call_fails(db_pool):
    session_id = await _new_session(db_pool, chat_id=8)
    telegram_bot = AsyncMock()
    telegram_bot.get_chat.side_effect = RuntimeError("Forbidden: bot was kicked")

    result = await composed.sync_chat_info(db_pool, telegram_bot, session_id)

    assert result == {"status": "unavailable"}


async def test_event_status_falls_back_to_the_place_as_it_was_written(db_pool):
    """Two things at once.

    The live gap: event_status read only facts['place'], and in one real
    session nothing ever wrote that key — the model chose "destination", then
    "event_name" — so the 📍 line was blank for the session's whole life while
    the places table held maps-resolved addresses.

    And the report repeats the group back to itself. A chat called
    "Море 20/11" had its place resolved to "The Old Man and the Sea", a
    restaurant, and the report announced that as the meeting place. The
    lookup stays for coordinates and the venue card; the report shows the
    words someone typed.
    """
    session_id = await _new_session(db_pool, chat_id=55)
    await db_pool.execute(
        "INSERT INTO places (session_id, name, address, lat, lon, query) "
        "VALUES ($1, 'The Old Man and the Sea', 'Kedem St 85', 32.0, 34.7, 'Море')",
        session_id,
    )

    result = await composed.event_status(db_pool, session_id)

    assert "📍 Место: Море" in result["report"]
    assert "The Old Man and the Sea" not in result["report"]


async def test_a_place_row_with_no_original_phrasing_still_shows_something(db_pool):
    """query is nullable — rows written before it was recorded have only the
    maps name. Showing that beats showing nothing."""
    session_id = await _new_session(db_pool, chat_id=58)
    await db_pool.execute(
        "INSERT INTO places (session_id, name, address, lat, lon) "
        "VALUES ($1, 'Hanania Meadow', 'Route 1', 32.0, 34.7)",
        session_id,
    )

    result = await composed.event_status(db_pool, session_id)

    assert "📍 Место: Hanania Meadow" in result["report"]


async def test_a_stated_place_beats_an_older_lookup(db_pool):
    """Someone naming a place in conversation is more current than a lookup
    done earlier in the session."""
    import bot.tools.core as core_tools

    session_id = await _new_session(db_pool, chat_id=56)
    await db_pool.execute(
        "INSERT INTO places (session_id, name, address, lat, lon, query) "
        "VALUES ($1, 'Old Spot', 'somewhere', 32.0, 34.7, 'old')",
        session_id,
    )
    await core_tools.remember_fact(db_pool, session_id, "destination", "Море")

    result = await composed.event_status(db_pool, session_id)

    assert "📍 Место: Море" in result["report"]
    assert "Old Spot" not in result["report"]


async def test_no_place_anywhere_omits_the_line(db_pool):
    session_id = await _new_session(db_pool, chat_id=57)

    result = await composed.event_status(db_pool, session_id)

    assert "📍" not in result["report"]
