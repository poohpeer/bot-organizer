"""The place line: a readable name, and a link on its own line under it.

Live, the report read

    📍 Место: Бен шемен, координаты 31.9460200, 34.9434050

The model had put the coordinates inside the name. That costs two things at
once: the line reads like a database row, and the name matches nothing in
`places`, so the coordinates that would have made a link are unreachable —
the report shows raw coordinates *and* no map.

A link needs entities, which is why bot/telegram_text.py exists. parse_mode
is set nowhere else on purpose: Telegram drops a whole message on unbalanced
entities, and names come from users.
"""

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

import bot.status_command as status_command
import bot.status_render as status_render
import bot.telegram_text as telegram_text
import bot.session as session
import bot.tools.core as core_tools
from bot.maps_links import maps_link, strip_coordinates

URL = "https://www.google.com/maps?q=31.94602,34.943405"


# --- the name -------------------------------------------------------------

@pytest.mark.parametrize("written,expected", [
    ("Бен шемен, координаты 31.9460200, 34.9434050", "Бен шемен"),
    ("Тель-Авив (31.946020, 34.943405)", "Тель-Авив"),
    ("Море [31.946020; 34.943405]", "Море"),
    ("поляна Ханания 31.946020,34.943405", "поляна Ханания"),
])
def test_coordinates_are_not_part_of_a_name(written, expected):
    assert strip_coordinates(written) == expected


@pytest.mark.parametrize("name", [
    "Бен шемен",
    "дом 12",
    "квартира 5, подъезд 2",
    # Nothing but coordinates: bot/router.py stores a pin this way on purpose
    # when nobody has named the place yet, and stripping it leaves nothing.
    "31.946020, 34.943405",
])
def test_a_name_that_is_not_coordinates_is_untouched(name):
    assert strip_coordinates(name) == name


async def test_the_stored_place_never_carries_coordinates(db_pool):
    """Enforced where it is written, not where it is read: several callers
    write this key and only one of them is the model."""
    await db_pool.execute(
        "INSERT INTO chats (chat_id, title) VALUES (-100, 'Chat') ON CONFLICT DO NOTHING"
    )
    active = await session.start_session(db_pool, chat_id=-100, activity_type="picnic")

    await core_tools.remember_fact(
        db_pool, active["id"], "place", "Бен шемен, координаты 31.9460200, 34.9434050"
    )

    facts = (await core_tools.get_facts(db_pool, active["id"], key="place"))["facts"]
    assert facts["place"] == "Бен шемен"


async def test_other_facts_are_left_exactly_as_written(db_pool):
    """Only the place key has a reader that depends on the exact string."""
    await db_pool.execute(
        "INSERT INTO chats (chat_id, title) VALUES (-100, 'Chat') ON CONFLICT DO NOTHING"
    )
    active = await session.start_session(db_pool, chat_id=-100, activity_type="picnic")
    said = "встречаемся у столба 31.946020, 34.943405"

    await core_tools.remember_fact(db_pool, active["id"], "как_найти", said)

    facts = (await core_tools.get_facts(db_pool, active["id"], key="как_найти"))["facts"]
    assert facts["как_найти"] == said


# --- the line -------------------------------------------------------------

def _place_block(**kwargs):
    rendered = status_render.render_status(
        participants_rendered="—", list_rendered="—", reminders_rendered="—", **kwargs
    )
    return telegram_text.strip_links(rendered).split("\n\n")[1]


def test_the_link_is_its_own_line_under_the_name():
    block = _place_block(place="Бен шемен", place_link=URL, event_date="31/08")

    assert block.splitlines() == ["📍 Место: Бен шемен", "🔗 Map", "📅 Дата: 31/08"]


def test_a_place_with_no_link_has_no_line_for_one():
    block = _place_block(place="Бен шемен", place_link=None, event_date=None)

    assert block == "📍 Место: Бен шемен"


def test_the_label_is_a_link_not_the_url():
    rendered = status_render.render_status(
        place="Бен шемен", place_link=URL, event_date=None,
        participants_rendered="—", list_rendered="—", reminders_rendered="—",
    )

    html = telegram_text.to_html(rendered)

    assert f'<a href="{URL}">🔗 Map</a>' in html
    assert "google.com/maps" not in telegram_text.strip_links(rendered), \
        "the raw URL is three lines long on a phone — that is what the label is for"


# --- sending --------------------------------------------------------------

def _message(chat_id=-100):
    return SimpleNamespace(
        chat=SimpleNamespace(id=chat_id, type="group"),
        from_user=SimpleNamespace(id=7),
    )


async def _with_place(db_pool, chat_id=-100):
    await db_pool.execute(
        "INSERT INTO chats (chat_id, title) VALUES ($1, 'Chat') ON CONFLICT DO NOTHING", chat_id
    )
    active = await session.start_session(db_pool, chat_id=chat_id, activity_type="picnic")
    await core_tools.remember_fact(db_pool, active["id"], "place", "Бен шемен")
    await db_pool.execute(
        "INSERT INTO places (session_id, name, address, lat, lon, query) "
        "VALUES ($1, 'Бен шемен', NULL, 31.94602, 34.943405, 'Бен шемен')",
        active["id"],
    )
    return active


async def test_status_sends_the_link_as_html(db_pool):
    await _with_place(db_pool)
    telegram_bot = AsyncMock()

    await status_command.handle_command(db_pool, telegram_bot, _message(), "status")

    sent = telegram_bot.send_message.await_args.kwargs
    assert sent["parse_mode"] == "HTML"
    assert f'<a href="{maps_link(31.94602, 34.943405)}">🔗 Map</a>' in sent["text"]


async def test_a_message_without_a_link_is_still_sent_as_plain_text(db_pool):
    """The property that makes this safe to add: nothing that works today
    starts going through an HTML parser."""
    await db_pool.execute(
        "INSERT INTO chats (chat_id, title) VALUES (-100, 'Chat') ON CONFLICT DO NOTHING"
    )
    active = await session.start_session(db_pool, chat_id=-100, activity_type="picnic")
    await core_tools.remember_fact(db_pool, active["id"], "place", "у Витька на даче")
    telegram_bot = AsyncMock()

    await status_command.handle_command(db_pool, telegram_bot, _message(), "status")

    sent = telegram_bot.send_message.await_args.kwargs
    assert "parse_mode" not in sent
    assert "&" not in sent["text"] and "<" not in sent["text"]


async def test_a_place_name_with_html_in_it_is_shown_not_interpreted(db_pool):
    """Names come from users. One "<" must not lose the whole message, which
    is exactly why parse_mode is set nowhere else."""
    active = await _with_place(db_pool)
    await core_tools.remember_fact(db_pool, active["id"], "place", "<b>Бен</b> & Co")
    await db_pool.execute(
        "INSERT INTO places (session_id, name, address, lat, lon, query) "
        "VALUES ($1, '<b>Бен</b> & Co', NULL, 31.94602, 34.943405, '<b>Бен</b> & Co')",
        active["id"],
    )
    telegram_bot = AsyncMock()

    await status_command.handle_command(db_pool, telegram_bot, _message(), "status")

    text = telegram_bot.send_message.await_args.kwargs["text"]
    assert "&lt;b&gt;Бен&lt;/b&gt; &amp; Co" in text
    assert text.count("<a href=") == 1, "the only tag is the one we wrote"


def test_a_url_we_would_not_follow_is_not_made_clickable():
    """The label still reads correctly; it simply is not a link."""
    assert telegram_text.link("javascript:alert(1)", "🔗 Map") == "🔗 Map"
    assert telegram_text.link(None, "🔗 Map") == "🔗 Map"


def test_markers_never_reach_a_person_as_control_characters():
    """Anywhere a message goes out without telegram_text — a log line, a
    reminder body — the label survives and the marker does not."""
    marked = "Место: Бен\n" + telegram_text.link(URL, "🔗 Map")

    plain = telegram_text.strip_links(marked)

    assert plain == "Место: Бен\n🔗 Map"
    assert not telegram_text.has_link(plain)
