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
import bot.tools.core as core_tools

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


# --- handle_active_message ---------------------------------------------------

async def _decision_log_count(db_pool, chat_id=-100):
    return await db_pool.fetchval("SELECT count(*) FROM decision_log WHERE chat_id = $1", chat_id)


async def _new_active_session(db_pool, chat_id=-100, activity_type="picnic"):
    await _ensure_chat(db_pool, chat_id=chat_id)
    row = await session.start_session(db_pool, chat_id=chat_id, activity_type=activity_type)
    return dict(row)


async def test_unaddressed_message_records_facts_and_stays_silent(db_pool, monkeypatch):
    active = await _new_active_session(db_pool)
    telegram_bot = AsyncMock()
    monkeypatch.setattr(router, "extract", AsyncMock(return_value={
        "list_items": ["помидоры"], "checked_off_items": [], "facts": [],
    }))
    run_tool_loop_mock = AsyncMock()
    monkeypatch.setattr(router, "run_tool_loop", run_tool_loop_mock)
    msg = _message("нужны помидоры", addressed=False)

    await router.handle_active_message(db_pool, telegram_bot, active, msg, BOT_ID, BOT_USERNAME)

    items = await db_pool.fetch(
        "SELECT name FROM list_items WHERE session_id = $1", active["id"]
    )
    assert [r["name"] for r in items] == ["помидоры"]
    telegram_bot.send_message.assert_not_awaited()
    run_tool_loop_mock.assert_not_awaited()

    refreshed = await session.get_active_session(db_pool, -100)
    assert refreshed["last_activity_at"] > active["last_activity_at"]
    assert await _decision_log_count(db_pool) == 1


async def test_addressed_explicit_stop_closes_and_summarizes(db_pool, monkeypatch):
    active = await _new_active_session(db_pool)
    telegram_bot = AsyncMock()
    monkeypatch.setattr(router, "classify", AsyncMock(return_value=True))
    msg = _message("всё, спасибо, свободен")

    await router.handle_active_message(db_pool, telegram_bot, active, msg, BOT_ID, BOT_USERNAME)

    row = await db_pool.fetchrow("SELECT status, closed_reason FROM sessions WHERE id = $1", active["id"])
    assert (row["status"], row["closed_reason"]) == ("closed", "explicit_stop")
    telegram_bot.send_message.assert_awaited_once()
    assert telegram_bot.send_message.await_args.kwargs["chat_id"] == -100
    assert await _decision_log_count(db_pool) == 1


async def test_addressed_stop_while_snoozed_still_closes(db_pool, monkeypatch):
    """The R4 case from the brief: an event silently moved, 'not yet' already
    answered (snooze running, closing_question_asked_at back to NULL), and an
    explicit human stop must override the snooze immediately."""
    active = await _new_active_session(db_pool)
    await db_pool.execute(
        "UPDATE sessions SET closing_question_snoozed_until = now() + interval '5 days' WHERE id = $1",
        active["id"],
    )
    active = dict(await session.get_active_session(db_pool, -100))
    assert active["closing_question_asked_at"] is None  # precondition: not an outstanding question

    telegram_bot = AsyncMock()
    monkeypatch.setattr(router, "classify", AsyncMock(return_value=True))
    msg = _message("больше не нужен, можешь отдыхать")

    await router.handle_active_message(db_pool, telegram_bot, active, msg, BOT_ID, BOT_USERNAME)

    row = await db_pool.fetchrow("SELECT status, closed_reason FROM sessions WHERE id = $1", active["id"])
    assert (row["status"], row["closed_reason"]) == ("closed", "explicit_stop")


async def test_various_stop_phrasings_all_close_via_the_classifier(db_pool, monkeypatch):
    telegram_bot = AsyncMock()
    monkeypatch.setattr(router, "classify", AsyncMock(return_value=True))

    for i, phrasing in enumerate([
        "мы закончили",
        "можешь отдыхать",
        "больше не нужен",
        "that's it, thanks",
    ]):
        chat_id = -200 - i
        active = await _new_active_session(db_pool, chat_id=chat_id)
        msg = _message(phrasing, chat_id=chat_id)

        await router.handle_active_message(db_pool, telegram_bot, active, msg, BOT_ID, BOT_USERNAME)

        row = await db_pool.fetchrow("SELECT status FROM sessions WHERE id = $1", active["id"])
        assert row["status"] == "closed", phrasing


async def test_addressed_closing_question_reply_yes_routes_to_record_closing_reply(db_pool, monkeypatch):
    active = await _new_active_session(db_pool)
    await db_pool.execute(
        "UPDATE sessions SET closing_question_asked_at = now() WHERE id = $1", active["id"]
    )
    active = dict(await session.get_active_session(db_pool, -100))
    assert active["closing_question_asked_at"] is not None

    telegram_bot = AsyncMock()
    monkeypatch.setattr(router, "extract", AsyncMock(return_value={"reply": "yes"}))
    run_tool_loop_mock = AsyncMock()
    monkeypatch.setattr(router, "run_tool_loop", run_tool_loop_mock)
    msg = _message("да, всё")

    await router.handle_active_message(db_pool, telegram_bot, active, msg, BOT_ID, BOT_USERNAME)

    row = await db_pool.fetchrow("SELECT status, closed_reason FROM sessions WHERE id = $1", active["id"])
    assert (row["status"], row["closed_reason"]) == ("closed", "closing_question_yes")
    run_tool_loop_mock.assert_not_awaited()
    telegram_bot.send_message.assert_awaited_once()
    assert await _decision_log_count(db_pool) == 1


async def test_addressed_closing_question_reply_no_stays_active(db_pool, monkeypatch):
    active = await _new_active_session(db_pool)
    await db_pool.execute(
        "UPDATE sessions SET closing_question_asked_at = now() WHERE id = $1", active["id"]
    )
    active = dict(await session.get_active_session(db_pool, -100))

    telegram_bot = AsyncMock()
    monkeypatch.setattr(router, "extract", AsyncMock(return_value={"reply": "no"}))
    run_tool_loop_mock = AsyncMock()
    monkeypatch.setattr(router, "run_tool_loop", run_tool_loop_mock)
    msg = _message("нет, ещё нужен")

    await router.handle_active_message(db_pool, telegram_bot, active, msg, BOT_ID, BOT_USERNAME)

    row = await db_pool.fetchrow(
        "SELECT status, closing_question_asked_at FROM sessions WHERE id = $1", active["id"]
    )
    assert row["status"] == "active"
    assert row["closing_question_asked_at"] is None
    run_tool_loop_mock.assert_not_awaited()
    telegram_bot.send_message.assert_awaited_once_with(chat_id=-100, text=router._STILL_ACTIVE_ACK)


async def test_addressed_yes_with_pending_confirmation_resolves_and_executes(db_pool, monkeypatch):
    active = await _new_active_session(db_pool)
    await core_tools.list_add(db_pool, active["id"], "tomatoes")
    proposed = await core_tools.propose_confirmation(
        db_pool, chat_id=-100, session_id=active["id"],
        action_type="list_remove_item", action_params={"name": "tomatoes"},
    )

    telegram_bot = AsyncMock()
    monkeypatch.setattr(router, "extract", AsyncMock(return_value={"reply": "yes"}))
    run_tool_loop_mock = AsyncMock()
    monkeypatch.setattr(router, "run_tool_loop", run_tool_loop_mock)
    msg = _message("да")

    await router.handle_active_message(db_pool, telegram_bot, active, msg, BOT_ID, BOT_USERNAME)

    confirmation_row = await db_pool.fetchrow(
        "SELECT status FROM pending_confirmations WHERE id = $1", proposed["confirmation_id"]
    )
    assert confirmation_row["status"] == "confirmed"
    remaining = await db_pool.fetch("SELECT name FROM list_items WHERE session_id = $1", active["id"])
    assert remaining == []
    run_tool_loop_mock.assert_not_awaited()
    telegram_bot.send_message.assert_awaited_once_with(chat_id=-100, text=router._CONFIRMED_DONE)


async def test_addressed_ordinary_request_runs_tool_loop_and_replies(db_pool, monkeypatch):
    active = await _new_active_session(db_pool)
    telegram_bot = AsyncMock()
    monkeypatch.setattr(router, "classify", AsyncMock(return_value=False))
    monkeypatch.setattr(router, "run_tool_loop", AsyncMock(return_value="Вот список: помидоры"))
    msg = _message("что у нас в списке")

    await router.handle_active_message(db_pool, telegram_bot, active, msg, BOT_ID, BOT_USERNAME)

    telegram_bot.send_message.assert_awaited_once_with(chat_id=-100, text="Вот список: помидоры")
    assert await _decision_log_count(db_pool) == 1


async def test_empty_tool_loop_reply_posts_nothing(db_pool, monkeypatch):
    active = await _new_active_session(db_pool)
    telegram_bot = AsyncMock()
    monkeypatch.setattr(router, "classify", AsyncMock(return_value=False))
    monkeypatch.setattr(router, "run_tool_loop", AsyncMock(return_value="   "))
    msg = _message("ладно проехали")

    await router.handle_active_message(db_pool, telegram_bot, active, msg, BOT_ID, BOT_USERNAME)

    telegram_bot.send_message.assert_not_awaited()
    assert await _decision_log_count(db_pool) == 1


async def test_tool_loop_failure_sends_fixed_fallback_message(db_pool, monkeypatch):
    active = await _new_active_session(db_pool)
    telegram_bot = AsyncMock()
    monkeypatch.setattr(router, "classify", AsyncMock(return_value=False))
    monkeypatch.setattr(router, "run_tool_loop", AsyncMock(side_effect=RuntimeError("boom")))
    msg = _message("???")

    await router.handle_active_message(db_pool, telegram_bot, active, msg, BOT_ID, BOT_USERNAME)

    telegram_bot.send_message.assert_awaited_once_with(chat_id=-100, text=router._FALLBACK_MESSAGE)
