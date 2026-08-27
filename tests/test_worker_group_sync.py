from types import SimpleNamespace
from unittest.mock import AsyncMock

import bot.session as session
import worker.group_sync as group_sync


async def _active_session(db_pool, chat_id=1, activity_type="picnic"):
    await db_pool.execute(
        "INSERT INTO chats (chat_id, title) VALUES ($1, 'Chat') ON CONFLICT DO NOTHING", chat_id
    )
    row = await session.start_session(db_pool, chat_id=chat_id, activity_type=activity_type)
    return row["id"]


def _telegram_bot(title="Chat", description="Original description", member_count=5):
    telegram_bot = AsyncMock()
    telegram_bot.get_chat.return_value = SimpleNamespace(title=title, description=description)
    telegram_bot.get_chat_member_count.return_value = member_count
    return telegram_bot


async def _mark_already_seen(db_pool, chat_id, title, description, checked_seconds_ago=120):
    """Seeds chats._seen columns as if a previous sync already ran, with
    info_checked_at pushed into the past so the interval gate doesn't skip
    the chat on the next call."""
    await db_pool.execute(
        """
        UPDATE chats SET title_seen = $2, description_seen = $3,
                          info_checked_at = now() - ($4 * interval '1 second')
        WHERE chat_id = $1
        """,
        chat_id, title, description, checked_seconds_ago,
    )


async def test_changed_description_produces_one_message_and_next_poll_produces_none(db_pool, monkeypatch):
    session_id = await _active_session(db_pool)
    await _mark_already_seen(db_pool, 1, "Chat", "Original description")
    telegram_bot = _telegram_bot(description="New description")
    monkeypatch.setattr(
        group_sync.group_info, "extract_event",
        AsyncMock(return_value={"activity_type": None, "event_date": None, "place": None}),
    )

    result = await group_sync.sync_group_info(db_pool, telegram_bot)

    assert result == {"announced": [1]}
    telegram_bot.send_message.assert_awaited_once()
    assert "New description" in telegram_bot.send_message.await_args.kwargs["text"]

    # Re-open the interval gate (as if 60s had passed) without changing the
    # info Telegram reports — the description is now what was last seen, so
    # this poll must announce nothing.
    await db_pool.execute(
        "UPDATE chats SET info_checked_at = now() - interval '120 seconds' WHERE chat_id = 1"
    )
    telegram_bot.send_message.reset_mock()

    result2 = await group_sync.sync_group_info(db_pool, telegram_bot)

    assert result2 == {"announced": []}
    telegram_bot.send_message.assert_not_awaited()


async def test_unchanged_chat_produces_nothing(db_pool):
    """Also covers the first-sighting case: a chat synced for the first time
    must not announce itself."""
    await _active_session(db_pool)
    telegram_bot = _telegram_bot()

    result = await group_sync.sync_group_info(db_pool, telegram_bot)

    assert result == {"announced": []}
    telegram_bot.send_message.assert_not_awaited()


async def test_dormant_chat_is_never_fetched_at_all(db_pool):
    """R5: a dormant chat is not being tracked, so touching it at all would be
    the bot speaking (or even just watching) unbidden."""
    await db_pool.execute("INSERT INTO chats (chat_id, title) VALUES (1, 'Chat')")
    telegram_bot = _telegram_bot()

    result = await group_sync.sync_group_info(db_pool, telegram_bot)

    assert result == {"announced": []}
    telegram_bot.get_chat.assert_not_awaited()
    telegram_bot.send_message.assert_not_awaited()


async def test_one_chats_get_chat_failing_does_not_stop_the_others(db_pool, monkeypatch):
    await _active_session(db_pool, chat_id=1)
    await _active_session(db_pool, chat_id=2)
    await _mark_already_seen(db_pool, 1, "Chat", "Original")
    await _mark_already_seen(db_pool, 2, "Chat", "Original")
    monkeypatch.setattr(
        group_sync.group_info, "extract_event",
        AsyncMock(return_value={"activity_type": None, "event_date": None, "place": None}),
    )

    telegram_bot = AsyncMock()

    async def get_chat(chat_id):
        if chat_id == 1:
            raise RuntimeError("Forbidden: bot was kicked")
        return SimpleNamespace(title="Chat", description="New description")

    telegram_bot.get_chat.side_effect = get_chat
    telegram_bot.get_chat_member_count.return_value = 5

    result = await group_sync.sync_group_info(db_pool, telegram_bot)

    assert result == {"announced": [2]}


async def test_the_announcement_names_what_actually_changed(db_pool, monkeypatch):
    await _active_session(db_pool)
    await _mark_already_seen(db_pool, 1, "Old title", "Same description")
    telegram_bot = _telegram_bot(title="New title", description="Same description")
    monkeypatch.setattr(
        group_sync.group_info, "extract_event",
        AsyncMock(return_value={"activity_type": None, "event_date": None, "place": None}),
    )

    await group_sync.sync_group_info(db_pool, telegram_bot)

    text = telegram_bot.send_message.await_args.kwargs["text"]
    assert "New title" in text
    assert "Same description" not in text


async def test_recently_checked_chat_is_skipped_until_the_interval_elapses(db_pool, monkeypatch):
    """Without this gate, GROUP_SYNC_INTERVAL_SECONDS's whole point — capping
    get_chat to once per chat per interval — silently stops applying."""
    await _active_session(db_pool)
    await db_pool.execute(
        "UPDATE chats SET title_seen = 'Chat', description_seen = 'Original', info_checked_at = now() "
        "WHERE chat_id = 1"
    )
    telegram_bot = _telegram_bot(description="New description")

    result = await group_sync.sync_group_info(db_pool, telegram_bot)

    assert result == {"announced": []}
    telegram_bot.get_chat.assert_not_awaited()


async def test_member_count_is_stored_on_every_pass_even_without_a_change(db_pool):
    await _active_session(db_pool)
    telegram_bot = _telegram_bot(member_count=12)

    await group_sync.sync_group_info(db_pool, telegram_bot)

    stored = await db_pool.fetchval("SELECT member_count FROM chats WHERE chat_id = 1")
    assert stored == 12


async def test_changed_info_updates_the_sessions_event_date_and_place_fact(db_pool, monkeypatch):
    session_id = await _active_session(db_pool)
    await _mark_already_seen(db_pool, 1, "Chat", "Old")
    telegram_bot = _telegram_bot(description="Едем 20 октября, поляна Ханания")
    monkeypatch.setattr(
        group_sync.group_info, "extract_event",
        AsyncMock(return_value={
            "activity_type": "пикник", "event_date": "2026-10-20", "place": "поляна Ханания",
        }),
    )

    await group_sync.sync_group_info(db_pool, telegram_bot)

    row = await db_pool.fetchrow("SELECT event_date FROM sessions WHERE id = $1", session_id)
    assert row["event_date"].isoformat() == "2026-10-20"
    facts = await db_pool.fetchrow(
        "SELECT value FROM facts WHERE session_id = $1 AND key = 'place'", session_id
    )
    assert facts["value"] == "поляна Ханания"
