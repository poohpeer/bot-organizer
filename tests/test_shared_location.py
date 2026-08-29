"""A location dropped in the chat becomes the event's place, with a link.

A location message carries no text, so it used to reach the ordinary
active-message path and be classified as an empty string: the pin was seen
and forgotten. It now sets the place, and the status report renders a map
link beside the name.

The guard here matters as much as the feature. Not every pin is the venue —
someone shares a shop, a station, where they are standing — and silently
replacing a place the group confirmed by conversation is the same kind of
destruction as overwriting an amount nobody asked to change.
"""

from types import SimpleNamespace
from unittest.mock import AsyncMock

import bot.router as router
import bot.session as session
import bot.tools.composed as composed
import bot.tools.core as core_tools
import bot.status_render as status_render
import bot.telegram_text as telegram_text
from bot.maps_links import maps_link

BOT_ID = 999
BOT_USERNAME = "orgbot"

LAT, LON = 32.062512, 34.771235


async def _active(db_pool, chat_id=-100):
    await db_pool.execute(
        "INSERT INTO chats (chat_id, title) VALUES ($1, 'Chat') ON CONFLICT DO NOTHING", chat_id
    )
    return await session.start_session(db_pool, chat_id=chat_id, activity_type="picnic")


def _location_message(*, venue=None, addressed=False, lat=LAT, lon=LON):
    location = SimpleNamespace(latitude=lat, longitude=lon)
    return SimpleNamespace(
        chat=SimpleNamespace(id=-100, type="supergroup"),
        from_user=SimpleNamespace(id=7, is_bot=False, full_name="Alex"),
        text=None, caption=None,
        location=None if venue is not None else location,
        venue=venue,
        entities=(), caption_entities=(),
        # addressed_to_bot reaches for these on any message with entities,
        # and a message without them is exactly what a pin is.
        parse_entity=lambda entity: "",
        parse_caption_entity=lambda entity: "",
        reply_to_message=SimpleNamespace(
            from_user=SimpleNamespace(id=BOT_ID)
        ) if addressed else None,
    )


def _venue(title="Маленькая прага", address="Дизенгоф 1"):
    return SimpleNamespace(
        title=title, address=address,
        location=SimpleNamespace(latitude=LAT, longitude=LON),
    )


async def _place_fact(db_pool, session_id):
    return (await core_tools.get_facts(db_pool, session_id, key="place"))["facts"].get("place")


async def test_a_shared_venue_becomes_the_place(db_pool):
    active = await _active(db_pool)

    handled = await router.handle_shared_location(
        db_pool, active, _location_message(venue=_venue()), BOT_ID, BOT_USERNAME
    )

    assert handled is True, "the caller must stop rather than read a message with no text"
    assert await _place_fact(db_pool, active["id"]) == "Маленькая прага"
    place = await composed.current_place(db_pool, active["id"])
    assert (place["lat"], place["lon"]) == (LAT, LON)


async def test_a_bare_pin_becomes_the_place_when_there_is_none(db_pool):
    """Nothing to destroy, so it is taken — and named after its own
    coordinates, since a pin says where without saying what."""
    active = await _active(db_pool)

    await router.handle_shared_location(
        db_pool, active, _location_message(), BOT_ID, BOT_USERNAME
    )

    assert await _place_fact(db_pool, active["id"]) == "32.062512, 34.771235"
    place = await composed.current_place(db_pool, active["id"])
    assert (place["lat"], place["lon"]) == (LAT, LON)


async def test_an_unaddressed_pin_does_not_replace_a_named_place(db_pool):
    """The destructive case. Someone shares a shop in a chat that already
    agreed on the picnic spot; the picnic spot must survive."""
    active = await _active(db_pool)
    await core_tools.remember_fact(db_pool, active["id"], "place", "Маленькая прага")

    handled = await router.handle_shared_location(
        db_pool, active, _location_message(lat=1.0, lon=2.0), BOT_ID, BOT_USERNAME
    )

    assert handled is True, "still a location — it must not fall through to the text path"
    assert await _place_fact(db_pool, active["id"]) == "Маленькая прага"
    place = await composed.current_place(db_pool, active["id"])
    assert place["lat"] is None, "and its coordinates must not be borrowed either"


async def test_a_pin_addressed_to_the_bot_does_replace_it(db_pool):
    """Asked for explicitly, so it is what the group wants."""
    active = await _active(db_pool)
    await core_tools.remember_fact(db_pool, active["id"], "place", "Маленькая прага")

    await router.handle_shared_location(
        db_pool, active, _location_message(addressed=True), BOT_ID, BOT_USERNAME
    )

    place = await composed.current_place(db_pool, active["id"])
    assert (place["lat"], place["lon"]) == (LAT, LON)
    assert place["name"] == "Маленькая прага", "the group's own wording survives the pin"


async def test_an_addressed_venue_over_a_named_place_renames_it(db_pool):
    active = await _active(db_pool)
    await core_tools.remember_fact(db_pool, active["id"], "place", "где-то у моря")

    await router.handle_shared_location(
        db_pool, active, _location_message(venue=_venue(title="Пляж Фришман"), addressed=True),
        BOT_ID, BOT_USERNAME,
    )

    assert await _place_fact(db_pool, active["id"]) == "Пляж Фришман"


async def test_an_ordinary_message_is_not_a_location(db_pool):
    active = await _active(db_pool)
    message = _location_message()
    message.location = None

    assert await router.handle_shared_location(
        db_pool, active, message, BOT_ID, BOT_USERNAME
    ) is False


async def test_a_second_pin_corrects_the_first(db_pool):
    """Pinning the same place again is a correction — the first pin was on
    the wrong side of the park — so the coordinates move rather than the
    insert being dropped."""
    active = await _active(db_pool)
    await router.handle_shared_location(
        db_pool, active, _location_message(venue=_venue()), BOT_ID, BOT_USERNAME
    )

    await router.handle_shared_location(
        db_pool, active,
        _location_message(venue=SimpleNamespace(
            title="Маленькая прага", address=None,
            location=SimpleNamespace(latitude=1.5, longitude=2.5)),
        ),
        BOT_ID, BOT_USERNAME,
    )

    place = await composed.current_place(db_pool, active["id"])
    assert (place["lat"], place["lon"]) == (1.5, 2.5)
    rows = await db_pool.fetchval(
        "SELECT count(*) FROM places WHERE session_id = $1", active["id"]
    )
    assert rows == 1, "a correction is not a second place"
    address = await db_pool.fetchval(
        "SELECT address FROM places WHERE session_id = $1", active["id"]
    )
    assert address == "Дизенгоф 1", "a bare pin must not blank out a known address"


# --- the link in the report ----------------------------------------------

async def test_the_status_report_links_the_place(db_pool):
    active = await _active(db_pool)
    await router.handle_shared_location(
        db_pool, active, _location_message(venue=_venue()), BOT_ID, BOT_USERNAME
    )

    report = (await composed.event_status(db_pool, active["id"]))["report"]

    # The link is its own line under the name, and its label is a word — see
    # tests/test_map_link.py for why.
    assert "📍 Место: Маленькая прага\n" in report
    assert telegram_text.link(maps_link(LAT, LON), status_render.MAP_LABEL) in report


async def test_a_place_with_no_coordinates_has_no_link(db_pool):
    active = await _active(db_pool)
    await core_tools.remember_fact(db_pool, active["id"], "place", "у Витька на даче")

    report = (await composed.event_status(db_pool, active["id"]))["report"]

    assert "📍 Место: у Витька на даче\n" in report + "\n"
    assert "google.com/maps" not in report


async def test_a_pin_for_something_else_is_not_linked_beside_the_place(db_pool):
    """places holds every resolved place, so taking "the most recent
    coordinates" would put a shop's pin next to the picnic's name."""
    active = await _active(db_pool)
    await core_tools.remember_fact(db_pool, active["id"], "place", "Маленькая прага")
    await db_pool.execute(
        "INSERT INTO places (session_id, name, address, lat, lon, query) "
        "VALUES ($1, 'Супермаркет', NULL, 1.0, 2.0, 'супермаркет')",
        active["id"],
    )

    report = (await composed.event_status(db_pool, active["id"]))["report"]

    assert "📍 Место: Маленькая прага" in report
    assert "google.com/maps" not in report
