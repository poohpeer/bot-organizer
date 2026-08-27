import datetime as dt
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

from telegram import Chat, ChatMemberLeft, ChatMemberMember, ChatMemberUpdated, Message, User

import bot.main as main
import bot.session as session
import bot.tools.core as core_tools

BOT_ID = 4242
BOT_USERNAME = "orgbot"

_BOT_USER = User(id=BOT_ID, first_name="Organizer", is_bot=True, username=BOT_USERNAME)
_HUMAN = User(id=7, first_name="Sasha", is_bot=False)
_OTHER_BOT = User(id=99, first_name="OtherBot", is_bot=True, username="otherbot")


def _message(from_user=_HUMAN, chat_id=-100, text="hi"):
    return Message(
        message_id=1, date=dt.datetime.now(dt.timezone.utc),
        chat=Chat(id=chat_id, type="group"), from_user=from_user, text=text,
    )


def _join_message(members, chat_id=-100, from_user=_HUMAN):
    return Message(
        message_id=1, date=dt.datetime.now(dt.timezone.utc),
        chat=Chat(id=chat_id, type="group"), from_user=from_user,
        new_chat_members=members,
    )


def _member_update(old_status, new_status, chat_id=-100, user=_BOT_USER):
    classes = {"left": ChatMemberLeft, "member": ChatMemberMember}
    return ChatMemberUpdated(
        chat=Chat(id=chat_id, type="group"), from_user=_HUMAN,
        date=dt.datetime.now(dt.timezone.utc),
        old_chat_member=classes[old_status](user=user),
        new_chat_member=classes[new_status](user=user),
    )


# --- route_update -------------------------------------------------------------

async def test_route_update_drops_duplicate(monkeypatch):
    pool = MagicMock()
    monkeypatch.setattr(main.dedup, "is_duplicate", AsyncMock(return_value=True))
    dormant = AsyncMock()
    active = AsyncMock()
    monkeypatch.setattr(main.router, "handle_dormant_message", dormant)
    monkeypatch.setattr(main.router, "handle_active_message", active)

    await main.route_update(pool, AsyncMock(), _message(), BOT_ID, BOT_USERNAME, update_id=1)

    dormant.assert_not_awaited()
    active.assert_not_awaited()


async def test_route_update_ignores_message_from_a_bot(monkeypatch):
    """Two bots addressing each other would otherwise loop forever."""
    pool = MagicMock()
    is_duplicate = AsyncMock(return_value=False)
    monkeypatch.setattr(main.dedup, "is_duplicate", is_duplicate)
    dormant = AsyncMock()
    monkeypatch.setattr(main.router, "handle_dormant_message", dormant)

    await main.route_update(
        pool, AsyncMock(), _message(from_user=_OTHER_BOT), BOT_ID, BOT_USERNAME, update_id=1
    )

    is_duplicate.assert_awaited_once_with(pool, 1)  # dedup still ran first
    dormant.assert_not_awaited()


async def test_route_update_dormant_chat_dispatches_to_dormant_handler(monkeypatch):
    pool = MagicMock()
    telegram_bot = AsyncMock()
    monkeypatch.setattr(main.dedup, "is_duplicate", AsyncMock(return_value=False))
    monkeypatch.setattr(main.session, "get_active_session", AsyncMock(return_value=None))
    dormant = AsyncMock()
    monkeypatch.setattr(main.router, "handle_dormant_message", dormant)
    msg = _message()

    await main.route_update(pool, telegram_bot, msg, BOT_ID, BOT_USERNAME, update_id=1)

    dormant.assert_awaited_once_with(pool, telegram_bot, msg, BOT_ID, BOT_USERNAME)


async def test_route_update_active_chat_dispatches_with_session_row(monkeypatch):
    pool = MagicMock()
    telegram_bot = AsyncMock()
    active_row = {"id": 5, "chat_id": -100}
    monkeypatch.setattr(main.dedup, "is_duplicate", AsyncMock(return_value=False))
    monkeypatch.setattr(main.session, "get_active_session", AsyncMock(return_value=active_row))
    active_handler = AsyncMock()
    monkeypatch.setattr(main.router, "handle_active_message", active_handler)
    msg = _message()

    await main.route_update(pool, telegram_bot, msg, BOT_ID, BOT_USERNAME, update_id=1)

    active_handler.assert_awaited_once_with(pool, telegram_bot, active_row, msg, BOT_ID, BOT_USERNAME)


# --- new_chat_members -------------------------------------------------------

async def _ensure_chat(db_pool, chat_id=-100):
    await db_pool.execute(
        "INSERT INTO chats (chat_id, title) VALUES ($1, 'Chat') ON CONFLICT DO NOTHING", chat_id
    )


async def test_new_chat_member_in_active_session_adds_participant_and_announces_once(db_pool, monkeypatch):
    await _ensure_chat(db_pool)
    active = await session.start_session(db_pool, chat_id=-100, activity_type="picnic")
    monkeypatch.setattr(main.dedup, "is_duplicate", AsyncMock(return_value=False))
    telegram_bot = AsyncMock()
    msg = _join_message([User(id=555, first_name="Nova", is_bot=False)])

    await main.route_update(db_pool, telegram_bot, msg, BOT_ID, BOT_USERNAME, update_id=1)

    telegram_bot.send_message.assert_awaited_once()
    assert telegram_bot.send_message.await_args.kwargs["chat_id"] == -100
    rows = await db_pool.fetch(
        "SELECT display_name, status, user_id FROM participants WHERE session_id = $1", active["id"]
    )
    assert len(rows) == 1
    assert (rows[0]["display_name"], rows[0]["status"], rows[0]["user_id"]) == ("Nova", "unknown", 555)


async def test_new_chat_member_in_dormant_chat_does_nothing(db_pool, monkeypatch):
    """R5: a dormant chat is not being tracked, so a join there is recorded
    nowhere and announced to nobody."""
    await _ensure_chat(db_pool)
    monkeypatch.setattr(main.dedup, "is_duplicate", AsyncMock(return_value=False))
    telegram_bot = AsyncMock()
    msg = _join_message([User(id=555, first_name="Nova", is_bot=False)])

    await main.route_update(db_pool, telegram_bot, msg, BOT_ID, BOT_USERNAME, update_id=1)

    telegram_bot.send_message.assert_not_awaited()


async def test_bot_joining_is_ignored(db_pool, monkeypatch):
    """Same reasoning route_update already applies to a message *sent* by a
    bot: a bot is never a person to nudge for confirmation."""
    await _ensure_chat(db_pool)
    active = await session.start_session(db_pool, chat_id=-100, activity_type="picnic")
    monkeypatch.setattr(main.dedup, "is_duplicate", AsyncMock(return_value=False))
    telegram_bot = AsyncMock()
    msg = _join_message([User(id=999, first_name="HelperBot", is_bot=True, username="helperbot")])

    await main.route_update(db_pool, telegram_bot, msg, BOT_ID, BOT_USERNAME, update_id=1)

    telegram_bot.send_message.assert_not_awaited()
    rows = await db_pool.fetch("SELECT id FROM participants WHERE session_id = $1", active["id"])
    assert rows == []


async def test_rejoin_of_an_already_listed_member_does_not_duplicate(db_pool, monkeypatch):
    await _ensure_chat(db_pool)
    active = await session.start_session(db_pool, chat_id=-100, activity_type="picnic")
    await core_tools.set_participant(db_pool, active["id"], "Nova", "confirmed", user_id=555)
    monkeypatch.setattr(main.dedup, "is_duplicate", AsyncMock(return_value=False))
    telegram_bot = AsyncMock()
    msg = _join_message([User(id=555, first_name="Nova", is_bot=False)])

    await main.route_update(db_pool, telegram_bot, msg, BOT_ID, BOT_USERNAME, update_id=1)

    rows = await db_pool.fetch("SELECT id FROM participants WHERE session_id = $1", active["id"])
    assert len(rows) == 1


# --- route_membership -----------------------------------------------------

async def test_route_membership_drops_duplicate(monkeypatch):
    pool = MagicMock()
    monkeypatch.setattr(main.dedup, "is_duplicate", AsyncMock(return_value=True))
    added = AsyncMock()
    get_active = AsyncMock()
    monkeypatch.setattr(main.router, "handle_bot_added", added)
    monkeypatch.setattr(main.session, "get_active_session", get_active)

    update = _member_update("left", "member")
    await main.route_membership(pool, AsyncMock(), update, BOT_ID, BOT_USERNAME, update_id=1)

    added.assert_not_awaited()
    get_active.assert_not_awaited()


async def test_route_membership_bot_added_greets_and_never_checks_for_removal(monkeypatch):
    pool = MagicMock()
    telegram_bot = AsyncMock()
    monkeypatch.setattr(main.dedup, "is_duplicate", AsyncMock(return_value=False))
    added = AsyncMock()
    get_active = AsyncMock()
    monkeypatch.setattr(main.router, "handle_bot_added", added)
    monkeypatch.setattr(main.session, "get_active_session", get_active)

    update = _member_update("left", "member")
    await main.route_membership(pool, telegram_bot, update, BOT_ID, BOT_USERNAME, update_id=1)

    added.assert_awaited_once_with(pool, telegram_bot, update, BOT_ID, BOT_USERNAME)
    get_active.assert_not_awaited()


async def test_route_membership_bot_removed_closes_active_session(monkeypatch):
    pool = MagicMock()
    telegram_bot = AsyncMock()
    monkeypatch.setattr(main.dedup, "is_duplicate", AsyncMock(return_value=False))
    # handle_bot_added runs for real: bot_was_added is False for a departure,
    # so it must be a genuine no-op (no greeting) rather than something we
    # have to trust a mock about.
    monkeypatch.setattr(main.session, "get_active_session", AsyncMock(return_value={"id": 9, "chat_id": -100}))
    close_session = AsyncMock(return_value=True)
    monkeypatch.setattr(main.session, "close_session", close_session)

    update = _member_update("member", "left")
    await main.route_membership(pool, telegram_bot, update, BOT_ID, BOT_USERNAME, update_id=1)

    close_session.assert_awaited_once_with(pool, 9, reason="explicit_stop")
    telegram_bot.send_message.assert_not_awaited()


async def test_route_membership_bot_removed_with_no_active_session_is_a_noop(monkeypatch):
    pool = MagicMock()
    telegram_bot = AsyncMock()
    monkeypatch.setattr(main.dedup, "is_duplicate", AsyncMock(return_value=False))
    monkeypatch.setattr(main.session, "get_active_session", AsyncMock(return_value=None))
    close_session = AsyncMock()
    monkeypatch.setattr(main.session, "close_session", close_session)

    update = _member_update("member", "left")
    await main.route_membership(pool, telegram_bot, update, BOT_ID, BOT_USERNAME, update_id=1)

    close_session.assert_not_awaited()


# --- post_init / main -------------------------------------------------------

async def test_post_init_resolves_bot_username_via_get_me(monkeypatch):
    monkeypatch.setenv("DATABASE_URL", "postgresql://postgres:test@localhost:5432/postgres")
    fake_pool = object()
    monkeypatch.setattr(main.db_pool_module, "create_pool", AsyncMock(return_value=fake_pool))
    monkeypatch.setattr(main.db_pool_module, "init_db", AsyncMock())

    app = SimpleNamespace(
        bot=AsyncMock(get_me=AsyncMock(return_value=SimpleNamespace(id=555, username="renamed_bot"))),
        bot_data={},
    )

    await main.post_init(app)

    app.bot.get_me.assert_awaited_once()
    assert app.bot_data["bot_id"] == 555
    assert app.bot_data["bot_username"] == "renamed_bot"
    assert app.bot_data["pool"] is fake_pool
