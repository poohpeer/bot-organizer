import datetime as dt
from unittest.mock import AsyncMock

from telegram import (
    Chat,
    ChatMemberAdministrator,
    ChatMemberLeft,
    ChatMemberMember,
    ChatMemberUpdated,
    Message,
    MessageEntity,
    User,
)

import bot.router as router
import bot.session as session

BOT_ID = 4242
BOT_USERNAME = "orgbot"

_BOT = User(id=BOT_ID, first_name="Organizer", is_bot=True, username=BOT_USERNAME)
_HUMAN = User(id=7, first_name="Sasha", is_bot=False)


def _utf16_offset(text: str, marker: str) -> int:
    return len(text[: text.index(marker)].encode("utf-16-le")) // 2


def _message(text, chat_id=-100, chat_title=None, addressed=True, reply_to=None):
    entities = []
    if addressed and reply_to is None:
        handle = f"@{BOT_USERNAME}"
        text = f"{handle} {text}"
        entities = [MessageEntity(type=MessageEntity.MENTION, offset=0, length=len(handle))]
    return Message(
        message_id=1,
        date=dt.datetime.now(dt.timezone.utc),
        chat=Chat(id=chat_id, type="group", title=chat_title),
        from_user=_HUMAN,
        text=text,
        entities=entities,
        reply_to_message=reply_to,
    )


def _member_update(old_status, new_status, chat_id=-100, user=_BOT):
    classes = {"left": ChatMemberLeft, "member": ChatMemberMember}

    def make(status):
        if status == "administrator":
            return ChatMemberAdministrator(
                user=user, can_be_edited=False, is_anonymous=False,
                can_manage_chat=False, can_delete_messages=False,
                can_manage_video_chats=False, can_restrict_members=False,
                can_promote_members=False, can_change_info=False,
                can_invite_users=False, can_post_stories=False,
                can_edit_stories=False, can_delete_stories=False,
            )
        return classes[status](user=user)

    return ChatMemberUpdated(
        chat=Chat(id=chat_id, type="group"), from_user=_HUMAN,
        date=dt.datetime.now(dt.timezone.utc),
        old_chat_member=make(old_status), new_chat_member=make(new_status),
    )


async def _ensure_chat(db_pool, chat_id=-100, title=None):
    await db_pool.execute(
        "INSERT INTO chats (chat_id, title) VALUES ($1, $2) ON CONFLICT DO NOTHING",
        chat_id, title,
    )


# --- handle_dormant_message ------------------------------------------------

async def test_unaddressed_message_calls_no_model_no_db_no_reply(db_pool, monkeypatch):
    telegram_bot = AsyncMock()
    extract_mock = AsyncMock()
    monkeypatch.setattr(router, "extract", extract_mock)
    msg = _message("надо купить помидоры", addressed=False)

    await router.handle_dormant_message(db_pool, telegram_bot, msg, BOT_ID, BOT_USERNAME)

    extract_mock.assert_not_awaited()
    telegram_bot.send_message.assert_not_awaited()


async def test_addressed_message_naming_activity_starts_session(db_pool, monkeypatch):
    await _ensure_chat(db_pool)
    telegram_bot = AsyncMock()
    monkeypatch.setattr(
        router, "extract",
        AsyncMock(return_value={"confident": True, "activity_type": "picnic"}),
    )
    msg = _message("давай отслеживать пикник")

    await router.handle_dormant_message(db_pool, telegram_bot, msg, BOT_ID, BOT_USERNAME)

    active = await session.get_active_session(db_pool, -100)
    assert active is not None
    assert active["activity_type"] == "picnic"
    telegram_bot.send_message.assert_awaited_once()
    assert telegram_bot.send_message.await_args.kwargs["chat_id"] == -100


async def test_event_date_taken_from_chat_title_when_only_there(db_pool, monkeypatch):
    await _ensure_chat(db_pool, title="Пикник 15 сентября")
    telegram_bot = AsyncMock()
    extract_mock = AsyncMock(return_value={
        "confident": True, "activity_type": "picnic",
        "event_date": "2026-09-15", "event_date_raw": "15 сентября",
    })
    monkeypatch.setattr(router, "extract", extract_mock)
    msg = _message("следи за пикником", chat_title="Пикник 15 сентября")

    await router.handle_dormant_message(db_pool, telegram_bot, msg, BOT_ID, BOT_USERNAME)

    # the title must actually reach the model as context
    call_text = extract_mock.await_args.args[1]
    assert "Пикник 15 сентября" in call_text

    active = await session.get_active_session(db_pool, -100)
    assert active["event_date"] == dt.date(2026, 9, 15)
    assert active["event_date_raw"] == "15 сентября"


async def test_vague_addressed_message_asks_and_starts_no_session(db_pool, monkeypatch):
    await _ensure_chat(db_pool)
    telegram_bot = AsyncMock()
    monkeypatch.setattr(router, "extract", AsyncMock(return_value={"confident": False}))
    msg = _message("привет")

    await router.handle_dormant_message(db_pool, telegram_bot, msg, BOT_ID, BOT_USERNAME)

    assert await session.get_active_session(db_pool, -100) is None
    telegram_bot.send_message.assert_awaited_once_with(chat_id=-100, text=router._ASK_WHAT_TO_TRACK)


async def test_session_already_active_tells_user_and_does_not_duplicate(db_pool, monkeypatch):
    await _ensure_chat(db_pool)
    await session.start_session(db_pool, chat_id=-100, activity_type="birthday")
    telegram_bot = AsyncMock()
    monkeypatch.setattr(
        router, "extract",
        AsyncMock(return_value={"confident": True, "activity_type": "picnic"}),
    )
    msg = _message("давай ещё и пикник")

    await router.handle_dormant_message(db_pool, telegram_bot, msg, BOT_ID, BOT_USERNAME)

    telegram_bot.send_message.assert_awaited_once_with(chat_id=-100, text=router._ALREADY_TRACKING)
    rows = await db_pool.fetch("SELECT id FROM sessions WHERE chat_id = -100 AND status = 'active'")
    assert len(rows) == 1


# --- handle_bot_added --------------------------------------------------------

async def test_bot_added_sends_one_greeting_and_no_session(db_pool):
    telegram_bot = AsyncMock()
    update = _member_update("left", "member")

    await router.handle_bot_added(db_pool, telegram_bot, update, BOT_ID, BOT_USERNAME)

    telegram_bot.send_message.assert_awaited_once()
    assert telegram_bot.send_message.await_args.kwargs["chat_id"] == -100
    assert await session.get_active_session(db_pool, -100) is None


async def test_bot_promoted_sends_nothing(db_pool):
    telegram_bot = AsyncMock()
    update = _member_update("member", "administrator")

    await router.handle_bot_added(db_pool, telegram_bot, update, BOT_ID, BOT_USERNAME)

    telegram_bot.send_message.assert_not_awaited()
