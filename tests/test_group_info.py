import datetime as dt
from types import SimpleNamespace
from unittest.mock import AsyncMock

import bot.group_info as group_info


async def _ensure_chat(db_pool, chat_id=1, title="Chat"):
    await db_pool.execute(
        "INSERT INTO chats (chat_id, title) VALUES ($1, $2) ON CONFLICT DO NOTHING",
        chat_id, title,
    )


# --- fetch --------------------------------------------------------------

async def test_fetch_returns_title_description_and_member_count():
    telegram_bot = AsyncMock()
    telegram_bot.get_chat.return_value = SimpleNamespace(title="Picnic squad", description="15 сентября, поляна")
    telegram_bot.get_chat_member_count.return_value = 9

    info = await group_info.fetch(telegram_bot, 1)

    assert info == {"title": "Picnic squad", "description": "15 сентября, поляна", "member_count": 9}


async def test_fetch_returns_empty_dict_when_get_chat_raises():
    """A polling job must not die because one chat is unreachable — this is
    the fail-closed contract mirroring bot.ai.classify.extract's {}."""
    telegram_bot = AsyncMock()
    telegram_bot.get_chat.side_effect = RuntimeError("Forbidden: bot was kicked")

    info = await group_info.fetch(telegram_bot, 1)

    assert info == {}


# --- changes_since_last_seen ---------------------------------------------

async def test_first_sighting_reports_no_change_and_stores_values(db_pool):
    """Both _seen columns are NULL the first time a chat is looked at; reporting
    a change here would make every chat announce itself on the worker's very
    first poll."""
    await _ensure_chat(db_pool)

    result = await group_info.changes_since_last_seen(
        db_pool, 1, {"title": "Picnic squad", "description": "15 сентября"}
    )

    assert result == {
        "title_changed": False,
        "description_changed": False,
        "previous": {"title": None, "description": None},
    }
    row = await db_pool.fetchrow(
        "SELECT title_seen, description_seen, info_checked_at FROM chats WHERE chat_id = 1"
    )
    assert row["title_seen"] == "Picnic squad"
    assert row["description_seen"] == "15 сентября"
    assert row["info_checked_at"] is not None


async def test_unchanged_second_call_reports_no_change(db_pool):
    await _ensure_chat(db_pool)
    info = {"title": "Picnic squad", "description": "15 сентября"}
    await group_info.changes_since_last_seen(db_pool, 1, info)

    result = await group_info.changes_since_last_seen(db_pool, 1, info)

    assert result["title_changed"] is False
    assert result["description_changed"] is False


async def test_changed_title_reports_exactly_that_not_description(db_pool):
    await _ensure_chat(db_pool)
    await group_info.changes_since_last_seen(
        db_pool, 1, {"title": "Picnic squad", "description": "15 сентября"}
    )

    result = await group_info.changes_since_last_seen(
        db_pool, 1, {"title": "Picnic squad v2", "description": "15 сентября"}
    )

    assert result["title_changed"] is True
    assert result["description_changed"] is False
    assert result["previous"] == {"title": "Picnic squad", "description": "15 сентября"}


async def test_description_going_from_set_to_empty_counts_as_change(db_pool):
    await _ensure_chat(db_pool)
    await group_info.changes_since_last_seen(
        db_pool, 1, {"title": "Picnic squad", "description": "15 сентября"}
    )

    result = await group_info.changes_since_last_seen(
        db_pool, 1, {"title": "Picnic squad", "description": ""}
    )

    assert result["title_changed"] is False
    assert result["description_changed"] is True


# --- extract_event --------------------------------------------------------

async def test_title_with_date_and_place_extracts_both(monkeypatch):
    monkeypatch.setattr(
        group_info, "extract",
        AsyncMock(return_value={
            "activity_type": "поход", "event_date": "2026-09-15", "place": "Ханания",
        }),
    )

    event = await group_info.extract_event(
        "Поход 15 сентября, поляна Ханания", None, dt.date(2026, 8, 24)
    )

    assert event == {"activity_type": "поход", "event_date": "2026-09-15", "place": "Ханания"}


async def test_title_with_neither_extracts_neither(monkeypatch):
    monkeypatch.setattr(group_info, "extract", AsyncMock(return_value={}))

    event = await group_info.extract_event("Общий чат", None, dt.date(2026, 8, 24))

    assert event == {"activity_type": None, "event_date": None, "place": None}


async def test_bare_date_resolves_against_the_passed_in_today(monkeypatch):
    """Mirrors 0001's S11 fix for reminders: without a reference "today" in the
    instruction, "4 июля" with no year resolves against the model's training
    data instead of the actual next 4 July. This checks our side of that
    contract — that `today` actually reaches the instruction — since the
    resolution itself happens inside a mocked model call."""
    extract_mock = AsyncMock(return_value={"event_date": "2026-07-04"})
    monkeypatch.setattr(group_info, "extract", extract_mock)

    event = await group_info.extract_event("Едем 4 июля", None, dt.date(2026, 6, 1))

    instruction = extract_mock.await_args.args[0]
    assert "2026-06-01" in instruction
    assert "next upcoming occurrence" in instruction
    assert event["event_date"] == "2026-07-04"


async def test_extract_returning_empty_dict_produces_all_none_rather_than_raising(monkeypatch):
    """extract() already fails closed to {} on any classifier failure; this
    checks extract_event doesn't reach past that with a .get() on a missing
    key or otherwise turn a failure into a crash."""
    monkeypatch.setattr(group_info, "extract", AsyncMock(return_value={}))

    event = await group_info.extract_event("Поход", "15 сентября", dt.date(2026, 8, 24))

    assert event == {"activity_type": None, "event_date": None, "place": None}
