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


async def test_a_change_is_never_posted_to_the_chat(db_pool, monkeypatch):
    """A group renaming its own chat can see that it did. Being told about it
    is noise — and the announcement was landing even when the extraction
    behind it had failed and nothing had actually been applied."""
    await _active_session(db_pool)
    await _mark_already_seen(db_pool, 1, "Chat", "Original description")
    telegram_bot = _telegram_bot(description="Едем на Море 20/11")
    monkeypatch.setattr(
        group_sync.group_info, "extract_event",
        AsyncMock(return_value={"answered": True, "activity_type": None,
                                "event_date": "2026-11-20", "place": "Море"}),
    )

    await group_sync.sync_group_info(db_pool, telegram_bot)

    telegram_bot.send_message.assert_not_awaited()


async def test_a_change_is_applied_once_and_not_again(db_pool, monkeypatch):
    session_id = await _active_session(db_pool)
    await _mark_already_seen(db_pool, 1, "Chat", "Original description")
    telegram_bot = _telegram_bot(description="Едем на Море 20/11")
    monkeypatch.setattr(
        group_sync.group_info, "extract_event",
        AsyncMock(return_value={"answered": True, "activity_type": None,
                                "event_date": "2026-11-20", "place": "Море"}),
    )

    result = await group_sync.sync_group_info(db_pool, telegram_bot)

    assert result == {"updated": [{"chat_id": 1, "event_date": "2026-11-20", "place": "Море"}]}

    # Re-open the interval gate without changing what Telegram reports.
    await db_pool.execute(
        "UPDATE chats SET info_checked_at = now() - interval '120 seconds' WHERE chat_id = 1"
    )

    assert await group_sync.sync_group_info(db_pool, telegram_bot) == {"updated": []}
    assert await db_pool.fetchval(
        "SELECT count(*) FROM facts WHERE session_id = $1 AND key = 'place'", session_id
    ) == 1


async def test_unchanged_chat_produces_nothing(db_pool):
    """Also covers the first-sighting case: a chat synced for the first time
    must not announce itself."""
    await _active_session(db_pool)
    telegram_bot = _telegram_bot()

    result = await group_sync.sync_group_info(db_pool, telegram_bot)

    assert result == {"updated": []}
    telegram_bot.send_message.assert_not_awaited()


async def test_dormant_chat_is_never_fetched_at_all(db_pool):
    """R5: a dormant chat is not being tracked, so touching it at all would be
    the bot speaking (or even just watching) unbidden."""
    await db_pool.execute("INSERT INTO chats (chat_id, title) VALUES (1, 'Chat')")
    telegram_bot = _telegram_bot()

    result = await group_sync.sync_group_info(db_pool, telegram_bot)

    assert result == {"updated": []}
    telegram_bot.get_chat.assert_not_awaited()
    telegram_bot.send_message.assert_not_awaited()


async def test_one_chats_get_chat_failing_does_not_stop_the_others(db_pool, monkeypatch):
    await _active_session(db_pool, chat_id=1)
    await _active_session(db_pool, chat_id=2)
    await _mark_already_seen(db_pool, 1, "Chat", "Original")
    await _mark_already_seen(db_pool, 2, "Chat", "Original")
    monkeypatch.setattr(
        group_sync.group_info, "extract_event",
        AsyncMock(return_value={"answered": True, "activity_type": None, "event_date": None, "place": None}),
    )

    telegram_bot = AsyncMock()

    async def get_chat(chat_id):
        if chat_id == 1:
            raise RuntimeError("Forbidden: bot was kicked")
        return SimpleNamespace(title="Chat", description="New description")

    telegram_bot.get_chat.side_effect = get_chat
    telegram_bot.get_chat_member_count.return_value = 5

    result = await group_sync.sync_group_info(db_pool, telegram_bot)

    assert result == {"updated": []}, "chat 2 says nothing about an event"
    telegram_bot.get_chat.assert_awaited()


async def test_a_classifier_that_did_not_answer_leaves_the_change_for_next_time(db_pool, monkeypatch):
    """The live loss. The title moved to "Маленькая прага 31/8", the old code
    marked it seen straight away, the classifier chain then answered 400, and
    the change was gone: the next pass saw no difference and the event never
    learnt the new place."""
    session_id = await _active_session(db_pool)
    await _mark_already_seen(db_pool, 1, "Chat", "Old")
    telegram_bot = _telegram_bot(title="Маленькая прага 31/8", description="Old")
    failed = AsyncMock(return_value={"answered": False, "activity_type": None,
                                     "event_date": None, "place": None})
    monkeypatch.setattr(group_sync.group_info, "extract_event", failed)

    assert await group_sync.sync_group_info(db_pool, telegram_bot) == {"updated": []}
    assert await db_pool.fetchval(
        "SELECT title_seen FROM chats WHERE chat_id = 1"
    ) == "Chat", "an unread change must not be recorded as handled"

    # The next pass, with the classifier working again.
    await db_pool.execute(
        "UPDATE chats SET info_checked_at = now() - interval '120 seconds' WHERE chat_id = 1"
    )
    monkeypatch.setattr(
        group_sync.group_info, "extract_event",
        AsyncMock(return_value={"answered": True, "activity_type": None,
                                "event_date": "2026-08-31", "place": "Маленькая прага"}),
    )

    await group_sync.sync_group_info(db_pool, telegram_bot)

    assert await db_pool.fetchval(
        "SELECT value FROM facts WHERE session_id = $1 AND key = 'place'", session_id
    ) == "Маленькая прага"


async def test_a_title_that_says_nothing_is_still_marked_seen(db_pool, monkeypatch):
    """Otherwise a chat called "Друзья" is re-read, and a classifier call
    spent on it, every single minute forever."""
    await _active_session(db_pool)
    await _mark_already_seen(db_pool, 1, "Chat", "Old")
    telegram_bot = _telegram_bot(title="Друзья", description="Old")
    monkeypatch.setattr(group_sync.group_info, "extract_event", AsyncMock(return_value={"answered": True, "activity_type": None, "event_date": None, "place": None}))

    await group_sync.sync_group_info(db_pool, telegram_bot)

    assert await db_pool.fetchval("SELECT title_seen FROM chats WHERE chat_id = 1") == "Друзья"


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

    assert result == {"updated": []}
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
            "answered": True, "activity_type": "пикник",
            "event_date": "2026-10-20", "place": "поляна Ханания",
        }),
    )

    await group_sync.sync_group_info(db_pool, telegram_bot)

    row = await db_pool.fetchrow("SELECT event_date FROM sessions WHERE id = $1", session_id)
    assert row["event_date"].isoformat() == "2026-10-20"
    facts = await db_pool.fetchrow(
        "SELECT value FROM facts WHERE session_id = $1 AND key = 'place'", session_id
    )
    assert facts["value"] == "поляна Ханания"


async def test_the_sync_learns_who_created_the_chat(db_pool, monkeypatch):
    """The only path that fills this in for a group the bot was already in
    when the feature shipped. Without it, "часовой пояс из создателя" would
    only ever work for chats added afterwards."""
    await _active_session(db_pool, chat_id=-1)
    telegram_bot = _telegram_bot()
    telegram_bot.get_chat_administrators.return_value = [
        SimpleNamespace(status="creator", user=SimpleNamespace(id=77)),
    ]

    await group_sync.sync_group_info(db_pool, telegram_bot)

    assert await db_pool.fetchval("SELECT creator_user_id FROM chats WHERE chat_id = -1") == 77
