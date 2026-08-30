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


# --- changed_fields / record_as_seen --------------------------------------

async def test_first_sighting_reports_no_change(db_pool):
    """Both _seen columns are NULL the first time a chat is looked at;
    reporting a change here would have every chat read as brand new on the
    worker's very first poll."""
    await _ensure_chat(db_pool)

    changed = await group_info.changed_fields(
        db_pool, 1, {"title": "Picnic squad", "description": "15 сентября"}
    )

    assert changed == set()


async def test_comparing_does_not_record(db_pool):
    """The split this function exists for. Comparing used to store in the
    same call, so a change was consumed the instant it was noticed — before
    anything had been done about it. Live, the classifier then failed and the
    new title was gone for good."""
    await _ensure_chat(db_pool)

    await group_info.changed_fields(db_pool, 1, {"title": "Море 20/11", "description": None})

    row = await db_pool.fetchrow(
        "SELECT title_seen, info_checked_at FROM chats WHERE chat_id = 1"
    )
    assert row["title_seen"] is None
    assert row["info_checked_at"] is None


async def test_recording_stores_what_was_seen(db_pool):
    await _ensure_chat(db_pool)

    await group_info.record_as_seen(
        db_pool, 1, {"title": "Picnic squad", "description": "15 сентября"}
    )

    row = await db_pool.fetchrow(
        "SELECT title_seen, description_seen, info_checked_at FROM chats WHERE chat_id = 1"
    )
    assert row["title_seen"] == "Picnic squad"
    assert row["description_seen"] == "15 сентября"
    assert row["info_checked_at"] is not None


async def test_unchanged_second_call_reports_no_change(db_pool):
    await _ensure_chat(db_pool)
    info = {"title": "Picnic squad", "description": "15 сентября"}
    await group_info.record_as_seen(db_pool, 1, info)

    assert await group_info.changed_fields(db_pool, 1, info) == set()


async def test_changed_title_reports_exactly_that_not_description(db_pool):
    await _ensure_chat(db_pool)
    await group_info.record_as_seen(
        db_pool, 1, {"title": "Picnic squad", "description": "15 сентября"}
    )

    changed = await group_info.changed_fields(
        db_pool, 1, {"title": "Picnic squad v2", "description": "15 сентября"}
    )

    assert changed == {"title"}


async def test_description_going_from_set_to_empty_counts_as_change(db_pool):
    await _ensure_chat(db_pool)
    await group_info.record_as_seen(
        db_pool, 1, {"title": "Picnic squad", "description": "15 сентября"}
    )

    changed = await group_info.changed_fields(
        db_pool, 1, {"title": "Picnic squad", "description": ""}
    )

    assert changed == {"description"}


# --- extract_event --------------------------------------------------------

async def test_title_with_date_and_place_extracts_both(monkeypatch):
    monkeypatch.setattr(
        group_info, "extract",
        AsyncMock(return_value={
            "activity_type": "поход", "event_date": "2026-09-15",
            "place": "Ханания", "place_kind": "природа",
        }),
    )

    event = await group_info.extract_event(
        "Поход 15 сентября, поляна Ханания", None, dt.date(2026, 8, 24)
    )

    assert event == {
        "answered": True, "activity_type": "поход", "event_date": "2026-09-15",
        "place": "Ханания", "place_kind": "природа",
    }


async def test_a_title_that_is_not_a_place_yields_no_place(monkeypatch):
    """The group's own name is not somewhere anyone can go. The model may
    still put something in `place`; place_kind is what the code acts on."""
    monkeypatch.setattr(
        group_info, "extract",
        AsyncMock(return_value={
            "activity_type": "", "event_date": "", "place": "Друзья", "place_kind": "нет",
        }),
    )

    event = await group_info.extract_event("Друзья", None, dt.date(2026, 8, 24))

    assert event["place"] is None
    assert event["answered"] is True


async def test_an_unrecognised_place_kind_is_treated_as_no(monkeypatch):
    """A place is stored only on a positive, known answer — a model inventing
    its own category must not slip a place through."""
    monkeypatch.setattr(
        group_info, "extract",
        AsyncMock(return_value={"place": "Что-то", "place_kind": "может быть"}),
    )

    event = await group_info.extract_event("Что-то", None, dt.date(2026, 8, 24))

    assert event["place"] is None


async def test_bare_date_resolves_against_the_passed_in_today(monkeypatch):
    """Mirrors 0001's S11 fix for reminders: without a reference "today" in the
    instruction, "4 июля" with no year resolves against the model's training
    data instead of the actual next 4 July. This checks our side of that
    contract — that `today` actually reaches the instruction — since the
    resolution itself happens inside a mocked model call."""
    extract_mock = AsyncMock(return_value={"event_date": "2026-07-04", "place_kind": "нет"})
    monkeypatch.setattr(group_info, "extract", extract_mock)

    event = await group_info.extract_event("Едем 4 июля", None, dt.date(2026, 6, 1))

    instruction = extract_mock.await_args.args[0]
    assert "2026-06-01" in instruction
    assert "next upcoming occurrence" in instruction
    assert event["event_date"] == "2026-07-04"


async def test_a_classifier_that_did_not_answer_is_distinguishable(monkeypatch):
    """extract() fails closed to {} for a transport error, a refused request
    and an unparseable answer alike. With the strict schema a successful call
    always carries every key, so {} means nobody answered — and the caller
    has to be able to tell that from "answered, and the text says nothing",
    or it marks a change handled that was never read."""
    monkeypatch.setattr(group_info, "extract", AsyncMock(return_value={}))

    event = await group_info.extract_event("Поход", "15 сентября", dt.date(2026, 8, 24))

    assert event["answered"] is False
    assert event["event_date"] is None and event["place"] is None


# --- apply_event ----------------------------------------------------------

async def _session(db_pool, chat_id=1):
    await _ensure_chat(db_pool, chat_id)
    return await db_pool.fetchval(
        "INSERT INTO sessions (chat_id, activity_type) VALUES ($1, 'picnic') RETURNING id",
        chat_id,
    )


async def test_a_new_date_and_place_are_written(db_pool):
    session_id = await _session(db_pool)

    applied = await group_info.apply_event(db_pool, session_id, {
        "answered": True, "event_date": "2026-11-20", "place": "Море",
    })

    assert applied == {"event_date": "2026-11-20", "place": "Море"}
    assert await db_pool.fetchval(
        "SELECT event_date FROM sessions WHERE id = $1", session_id
    ) == dt.date(2026, 11, 20)
    assert await db_pool.fetchval(
        "SELECT value FROM facts WHERE session_id = $1 AND key = 'place'", session_id
    ) == "Море"


async def test_unchanged_values_are_not_written_again(db_pool):
    """facts rows are append-only and get_facts takes the newest per key, so
    re-recording an unchanged place on every touch of the title grows a pile
    of identical rows for no reason."""
    session_id = await _session(db_pool)
    event = {"answered": True, "event_date": "2026-11-20", "place": "Море"}
    await group_info.apply_event(db_pool, session_id, event)

    applied = await group_info.apply_event(db_pool, session_id, event)

    assert applied == {}
    assert await db_pool.fetchval(
        "SELECT count(*) FROM facts WHERE session_id = $1 AND key = 'place'", session_id
    ) == 1


async def test_only_the_field_that_moved_is_written(db_pool):
    session_id = await _session(db_pool)
    await group_info.apply_event(db_pool, session_id, {
        "answered": True, "event_date": "2026-11-20", "place": "Море",
    })

    applied = await group_info.apply_event(db_pool, session_id, {
        "answered": True, "event_date": "2026-11-21", "place": "Море",
    })

    assert applied == {"event_date": "2026-11-21"}
    assert await db_pool.fetchval(
        "SELECT count(*) FROM facts WHERE session_id = $1 AND key = 'place'", session_id
    ) == 1


async def test_a_malformed_date_is_ignored_rather_than_raising(db_pool):
    """The classifier is asked for ISO-8601 and is not guaranteed to comply.
    One bad date must not take down a timer pass for every other chat."""
    session_id = await _session(db_pool)

    applied = await group_info.apply_event(db_pool, session_id, {
        "answered": True, "event_date": "20 ноября", "place": "Море",
    })

    assert applied == {"place": "Море"}
    assert await db_pool.fetchval(
        "SELECT event_date FROM sessions WHERE id = $1", session_id
    ) is None


async def test_nothing_found_writes_nothing(db_pool):
    session_id = await _session(db_pool)

    applied = await group_info.apply_event(db_pool, session_id, {
        "answered": True, "event_date": None, "place": None,
    })

    assert applied == {}


# --- Who created the group --------------------------------------------------


def _admins(*members):
    telegram_bot = AsyncMock()
    telegram_bot.get_chat_administrators.return_value = list(members)
    return telegram_bot


def _member(status, user_id):
    return SimpleNamespace(status=status, user=SimpleNamespace(id=user_id))


async def test_the_creator_is_learned_and_remembered(db_pool):
    """Telegram has no field for it on the chat and no notification when it
    changes — getChatAdministrators is the only way to find out, and the
    creator's own timezone is what the chat falls back to."""
    await db_pool.execute("INSERT INTO chats (chat_id, title) VALUES (-1, 'Chat')")
    telegram_bot = _admins(_member("administrator", 5), _member("creator", 77))

    assert await group_info.ensure_creator_known(db_pool, telegram_bot, -1) == 77

    assert await db_pool.fetchval("SELECT creator_user_id FROM chats WHERE chat_id = -1") == 77


async def test_the_creator_is_asked_for_only_once(db_pool):
    """This runs on a timer for every chat with an active session. An owner
    change is rare enough not to be worth an API call every single sync."""
    await db_pool.execute("INSERT INTO chats (chat_id, title) VALUES (-1, 'Chat')")
    telegram_bot = _admins(_member("creator", 77))

    await group_info.ensure_creator_known(db_pool, telegram_bot, -1)
    await group_info.ensure_creator_known(db_pool, telegram_bot, -1)

    assert telegram_bot.get_chat_administrators.await_count == 1


async def test_a_chat_the_bot_has_never_written_down_still_records_its_creator(db_pool):
    """The bot learns this the moment it is added, which can be before the
    chat has a row at all."""
    telegram_bot = _admins(_member("creator", 77))

    assert await group_info.ensure_creator_known(db_pool, telegram_bot, -1) == 77

    assert await db_pool.fetchval("SELECT creator_user_id FROM chats WHERE chat_id = -1") == 77


async def test_a_private_chat_is_never_asked_about_administrators(db_pool):
    telegram_bot = _admins(_member("creator", 77))

    assert await group_info.ensure_creator_known(db_pool, telegram_bot, 42) is None

    telegram_bot.get_chat_administrators.assert_not_awaited()


async def test_a_group_with_no_reachable_owner_is_not_an_error(db_pool):
    """The creator can have left a supergroup. Nothing to record, nothing to
    fail — the chat simply keeps whatever zone it had."""
    await db_pool.execute("INSERT INTO chats (chat_id, title) VALUES (-1, 'Chat')")

    assert await group_info.ensure_creator_known(db_pool, _admins(_member("administrator", 5)), -1) is None

    failing = AsyncMock()
    failing.get_chat_administrators.side_effect = RuntimeError("kicked")
    assert await group_info.ensure_creator_known(db_pool, failing, -1) is None
