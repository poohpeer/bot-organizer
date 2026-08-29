"""A navigator link is taken exactly as sent.

Live, someone shared a short Google Maps link and the bot answered:

    Не смог открыть эту короткую ссылку — карты её не раскрывают. Пришли,
    пожалуйста, название места или точку, которая открывается (или полный
    адрес), и я поправлю локацию.

The link opens perfectly well on the phone of whoever receives it. Whether
maps can expand it says nothing about whether it works, so the lookup — and
the question that followed it — turned a working link into a conversation.

Handled in code rather than by the model because there is nothing to decide:
a Waze or Maps link in an organizing chat is where the event is, and the
whole job is to keep it and say so.
"""

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

import bot.router as router
import bot.session as session
import bot.status_render as status_render
import bot.telegram_text as telegram_text
import bot.tools.composed as composed
import bot.tools.core as core_tools
from bot.maps_links import find_map_url

SHORT = "https://maps.app.goo.gl/aBcD1234"
WAZE = "https://waze.com/ul/hsv8k7dz4h"


def _message(text, chat_id=-100):
    return SimpleNamespace(
        chat=SimpleNamespace(id=chat_id, type="supergroup"),
        from_user=SimpleNamespace(id=7, is_bot=False, full_name="Alex"),
        text=text, caption=None,
    )


async def _active(db_pool, chat_id=-100):
    await db_pool.execute(
        "INSERT INTO chats (chat_id, title) VALUES ($1, 'Chat') ON CONFLICT DO NOTHING", chat_id
    )
    return await session.start_session(db_pool, chat_id=chat_id, activity_type="picnic")


# --- what counts as one ---------------------------------------------------

@pytest.mark.parametrize("text,expected", [
    (SHORT, SHORT),
    ("вот сюда: " + WAZE + ".", WAZE),
    ("Бен шемен https://www.google.com/maps?q=31.94602,34.943405",
     "https://www.google.com/maps?q=31.94602,34.943405"),
    ("https://yandex.ru/maps/-/CDbBrK", "https://yandex.ru/maps/-/CDbBrK"),
    ("https://maps.apple.com/?ll=31.946,34.943", "https://maps.apple.com/?ll=31.946,34.943"),
    # A navigator nobody listed: a coordinate pair in the URL is enough.
    ("https://randomnav.io/x/31.946020,34.943405", "https://randomnav.io/x/31.946020,34.943405"),
])
def test_a_navigator_link_is_recognised(text, expected):
    assert find_map_url(text) == expected


@pytest.mark.parametrize("text", [
    "посмотрите https://example.com/foo",
    "нет ссылки вообще",
    "https://github.com/poohpeer/bot-organizer",
    "",
])
def test_an_ordinary_link_is_not_a_place(text):
    assert find_map_url(text) is None


# --- taking it ------------------------------------------------------------

async def test_the_link_is_stored_exactly_as_sent(db_pool):
    """Not expanded, not resolved, not normalised. A short link points at a
    pin no lookup would have found."""
    active = await _active(db_pool)
    telegram_bot = AsyncMock()

    handled = await router.handle_shared_map_link(
        db_pool, telegram_bot, active, _message(SHORT)
    )

    assert handled is True
    assert await db_pool.fetchval(
        "SELECT place_url FROM sessions WHERE id = $1", active["id"]
    ) == SHORT


async def test_it_says_it_added_and_asks_nothing(db_pool):
    active = await _active(db_pool)
    telegram_bot = AsyncMock()

    await router.handle_shared_map_link(db_pool, telegram_bot, active, _message(SHORT))

    text = telegram_bot.send_message.await_args.kwargs["text"]
    assert text == router.LINK_ADDED
    assert "?" not in text


async def test_no_lookup_is_made(db_pool, monkeypatch):
    """The whole point. maps_lookup is what produced the refusal."""
    import bot.tools.external as external

    def boom(*args, **kwargs):
        raise AssertionError("a link is taken as given, never looked up")

    monkeypatch.setattr(external, "maps_lookup", boom)
    active = await _active(db_pool)

    await router.handle_shared_map_link(db_pool, AsyncMock(), active, _message(SHORT))


async def test_an_existing_place_keeps_its_name(db_pool):
    """Additive: a link is not a reason to rename anything."""
    active = await _active(db_pool)
    await core_tools.remember_fact(db_pool, active["id"], "place", "Бен шемен")

    await router.handle_shared_map_link(db_pool, AsyncMock(), active, _message(SHORT))

    place = await composed.current_place(db_pool, active["id"])
    assert place["name"] == "Бен шемен"
    assert place["url"] == SHORT


async def test_text_beside_the_link_does_not_rename_an_existing_place(db_pool):
    """The destructive reading. "давайте лучше сюда <link>" must not turn the
    place into "давайте лучше сюда" — the group named it already, and a link
    is not a rename."""
    active = await _active(db_pool)
    await core_tools.remember_fact(db_pool, active["id"], "place", "Бен шемен")

    await router.handle_shared_map_link(
        db_pool, AsyncMock(), active, _message(f"давайте лучше сюда {WAZE}")
    )

    place = await composed.current_place(db_pool, active["id"])
    assert place["name"] == "Бен шемен"
    assert place["url"] == WAZE


async def test_a_name_is_taken_only_when_there_is_none(db_pool):
    active = await _active(db_pool)
    telegram_bot = AsyncMock()

    await router.handle_shared_map_link(
        db_pool, telegram_bot, active, _message(f"Бен шемен {SHORT}")
    )

    place = await composed.current_place(db_pool, active["id"])
    assert place["name"] == "Бен шемен"
    assert telegram_bot.send_message.await_args.kwargs["text"] == (
        router.LINK_ADDED_WITH_NAME.format(name="Бен шемен")
    )


async def test_a_bare_link_with_no_place_yet_invents_no_name(db_pool):
    active = await _active(db_pool)

    await router.handle_shared_map_link(db_pool, AsyncMock(), active, _message(SHORT))

    place = await composed.current_place(db_pool, active["id"])
    assert place["name"] is None
    assert place["url"] == SHORT


async def test_an_ordinary_message_falls_through(db_pool):
    active = await _active(db_pool)
    telegram_bot = AsyncMock()

    handled = await router.handle_shared_map_link(
        db_pool, telegram_bot, active, _message("а во сколько встречаемся?")
    )

    assert handled is False
    telegram_bot.send_message.assert_not_awaited()


# --- in the report --------------------------------------------------------

async def test_the_report_links_the_url_that_was_sent(db_pool):
    active = await _active(db_pool)
    await core_tools.remember_fact(db_pool, active["id"], "place", "Бен шемен")
    await router.handle_shared_map_link(db_pool, AsyncMock(), active, _message(WAZE))

    report = (await composed.event_status(db_pool, active["id"]))["report"]

    assert "📍 Место: Бен шемен\n" in report
    assert telegram_text.link(WAZE, status_render.MAP_LABEL) in report


async def test_a_sent_link_beats_one_built_from_coordinates(db_pool):
    """They took the trouble to send that exact link, and a short link often
    points at a pin no lookup would have found."""
    active = await _active(db_pool)
    await core_tools.remember_fact(db_pool, active["id"], "place", "Бен шемен")
    await db_pool.execute(
        "INSERT INTO places (session_id, name, address, lat, lon, query) "
        "VALUES ($1, 'Бен шемен', NULL, 31.94602, 34.943405, 'Бен шемен')",
        active["id"],
    )

    await router.handle_shared_map_link(db_pool, AsyncMock(), active, _message(WAZE))

    report = (await composed.event_status(db_pool, active["id"]))["report"]
    assert telegram_text.link(WAZE, status_render.MAP_LABEL) in report
    assert "google.com/maps" not in report


async def test_a_second_link_replaces_the_first(db_pool):
    active = await _active(db_pool)
    await router.handle_shared_map_link(db_pool, AsyncMock(), active, _message(SHORT))

    await router.handle_shared_map_link(db_pool, AsyncMock(), active, _message(WAZE))

    place = await composed.current_place(db_pool, active["id"])
    assert place["url"] == WAZE
