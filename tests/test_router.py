import datetime as dt
from zoneinfo import ZoneInfo
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
from telegram.error import Forbidden

import bot.router as router
import bot.session as session
import bot.tools.core as core_tools

BOT_ID = 4242
BOT_USERNAME = "orgbot"

_BOT = User(id=BOT_ID, first_name="Organizer", is_bot=True, username=BOT_USERNAME)
_HUMAN = User(id=7, first_name="Sasha", is_bot=False)


async def test_session_bound_registry_forces_active_session_and_sender_identity():
    seen = {}

    async def set_participant(**kwargs):
        seen.update(kwargs)
        return {"status": "ok"}

    registry = router._bind_session_context({"set_participant": set_participant}, 1, _HUMAN)

    result = await registry["set_participant"](
        session_id=14, display_name="Я", status="confirmed"
    )

    assert result == {"status": "ok"}
    assert seen == {
        "session_id": 1,
        "display_name": "Sasha",
        "status": "confirmed",
        "user_id": 7,
    }


async def test_bind_session_context_forces_current_user_id_for_send_private_message():
    """R5: current_user_id is the recipient of a private message — it must
    come from the router's own record of who is actually talking, never from
    the model, or a model-chosen recipient could DM anyone in any chat the
    bot has seen (the same hole chat_id closed in 0001's S4/S6)."""
    seen = {}

    async def send_private_message(**kwargs):
        seen.update(kwargs)
        return {"status": "ok"}

    registry = router._bind_session_context({"send_private_message": send_private_message}, 1, _HUMAN)

    await registry["send_private_message"](text="hello")

    assert seen == {"session_id": 1, "current_user_id": 7, "text": "hello"}


async def test_bind_session_context_binds_current_user_id_none_when_no_sender():
    """A channel post has no from_user. Binding None here — rather than
    skipping the bind or crashing — is what lets send_private_message report
    'failed' instead of the router blowing up before the tool ever runs."""
    seen = {}

    async def send_private_message(**kwargs):
        seen.update(kwargs)
        return {"status": "ok"}

    registry = router._bind_session_context({"send_private_message": send_private_message}, 1, None)

    await registry["send_private_message"](text="hello")

    assert seen["current_user_id"] is None


def test_has_visible_text_rejects_zero_width_only_reply():
    assert router._has_visible_text("\u200b\u200b") is False
    assert router._has_visible_text("  \u200b\n") is False
    assert router._has_visible_text("\u200bок") is True


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


async def test_addressed_message_registers_a_chat_seen_for_the_first_time(db_pool, monkeypatch):
    """sessions.chat_id is a foreign key into chats; nothing upstream of the
    router is guaranteed to have inserted that row yet."""
    telegram_bot = AsyncMock()
    monkeypatch.setattr(
        router, "extract",
        AsyncMock(return_value={"confident": True, "activity_type": "picnic"}),
    )
    msg = _message("давай отслеживать пикник", chat_title="New Chat")

    await router.handle_dormant_message(db_pool, telegram_bot, msg, BOT_ID, BOT_USERNAME)

    assert await session.get_active_session(db_pool, -100) is not None


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

def _no_group_info(monkeypatch):
    """Stubs group_info.fetch/extract_event to report nothing.

    handle_bot_added now always calls these to build the greeting; without a
    stub, an AsyncMock telegram_bot's get_chat would happily return another
    AsyncMock rather than raising, so group_info.fetch would not fail closed
    to {} — and extract_event would go on to make a real classifier call,
    which tests must never do.
    """
    monkeypatch.setattr(router.group_info, "fetch", AsyncMock(return_value={}))
    monkeypatch.setattr(
        router.group_info, "extract_event",
        AsyncMock(return_value={"activity_type": None, "event_date": None, "place": None}),
    )


async def test_bot_added_sends_one_greeting_and_no_session(db_pool, monkeypatch):
    _no_group_info(monkeypatch)
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


async def test_bot_added_with_bare_title_sends_todays_plain_greeting(db_pool, monkeypatch):
    """R7's last criterion, applied to the greeting: a title/description with
    nothing stated must not scaffold an empty "Поездка: не указано" — the
    greeting is exactly what it was before this story."""
    _no_group_info(monkeypatch)
    telegram_bot = AsyncMock()
    update = _member_update("left", "member")

    await router.handle_bot_added(db_pool, telegram_bot, update, BOT_ID, BOT_USERNAME)

    telegram_bot.send_message.assert_awaited_once_with(
        chat_id=-100, text=router._GREETING_TEMPLATE.format(mention=f"@{BOT_USERNAME}")
    )


async def test_bot_added_with_rich_info_names_activity_date_place_and_count(db_pool, monkeypatch):
    monkeypatch.setattr(
        router.group_info, "fetch",
        AsyncMock(return_value={
            "title": "Поход выходного дня", "description": "15 сентября, поляна Ханания",
            "member_count": 9,
        }),
    )
    monkeypatch.setattr(
        router.group_info, "extract_event",
        AsyncMock(return_value={
            "activity_type": "поход", "event_date": "2026-09-15", "place": "поляна Ханания",
        }),
    )
    telegram_bot = AsyncMock()
    update = _member_update("left", "member")

    await router.handle_bot_added(db_pool, telegram_bot, update, BOT_ID, BOT_USERNAME)

    text = telegram_bot.send_message.await_args.kwargs["text"]
    assert "поход" in text
    assert "15.09.2026" in text
    assert "поляна Ханания" in text
    assert "9" in text
    assert await session.get_active_session(db_pool, -100) is None


async def test_bot_added_greeting_never_claims_to_know_member_names(db_pool, monkeypatch):
    """It has a number from get_chat_member_count, not a roster — the
    greeting must read that way (R7)."""
    monkeypatch.setattr(
        router.group_info, "fetch",
        AsyncMock(return_value={"title": "Поход", "description": "15 сентября", "member_count": 9}),
    )
    monkeypatch.setattr(
        router.group_info, "extract_event",
        AsyncMock(return_value={"activity_type": "поход", "event_date": "2026-09-15", "place": None}),
    )
    telegram_bot = AsyncMock()
    update = _member_update("left", "member")

    await router.handle_bot_added(db_pool, telegram_bot, update, BOT_ID, BOT_USERNAME)

    text = telegram_bot.send_message.await_args.kwargs["text"].lower()
    assert "участник" not in text


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


async def test_markdown_in_tool_loop_reply_is_converted_before_sending(db_pool, monkeypatch):
    active = await _new_active_session(db_pool)
    telegram_bot = AsyncMock()
    monkeypatch.setattr(router, "classify", AsyncMock(return_value=False))
    monkeypatch.setattr(router, "run_tool_loop", AsyncMock(return_value="**Куда:** Ben Shemen"))
    msg = _message("куда едем")

    await router.handle_active_message(db_pool, telegram_bot, active, msg, BOT_ID, BOT_USERNAME)

    telegram_bot.send_message.assert_awaited_once_with(chat_id=-100, text="Куда: Ben Shemen")


async def test_markdown_wrapped_silent_sentinel_still_silences(db_pool, monkeypatch):
    """A model answering with '**<silent>**' instead of the bare sentinel must
    still be silenced — the conversion has to run before the sentinel check,
    not after, or the bold markers leave the comparison never matching."""
    active = await _new_active_session(db_pool)
    telegram_bot = AsyncMock()
    monkeypatch.setattr(router, "classify", AsyncMock(return_value=False))
    monkeypatch.setattr(router, "run_tool_loop", AsyncMock(return_value="**<silent>**"))
    msg = _message("записал")

    await router.handle_active_message(db_pool, telegram_bot, active, msg, BOT_ID, BOT_USERNAME)

    telegram_bot.send_message.assert_not_awaited()


async def test_a_reply_with_no_markdown_passes_through_unchanged(db_pool, monkeypatch):
    active = await _new_active_session(db_pool)
    telegram_bot = AsyncMock()
    monkeypatch.setattr(router, "classify", AsyncMock(return_value=False))
    monkeypatch.setattr(router, "run_tool_loop", AsyncMock(return_value="Готово, записал."))
    msg = _message("ладно")

    await router.handle_active_message(db_pool, telegram_bot, active, msg, BOT_ID, BOT_USERNAME)

    telegram_bot.send_message.assert_awaited_once_with(chat_id=-100, text="Готово, записал.")


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


async def test_two_simultaneous_stops_post_one_summary(db_pool, monkeypatch):
    """close_session returns False for whoever lost the race, precisely so the
    loser doesn't post a second closing summary."""
    import asyncio

    active = await _new_active_session(db_pool)
    monkeypatch.setattr(router, "classify", AsyncMock(return_value=True))
    monkeypatch.setattr(router, "_summarize_session", AsyncMock(return_value="ИТОГИ"))
    telegram_bot = AsyncMock()

    await asyncio.gather(
        router.handle_active_message(db_pool, telegram_bot, active, _message("@orgbot всё"), BOT_ID, BOT_USERNAME),
        router.handle_active_message(db_pool, telegram_bot, active, _message("@orgbot спасибо"), BOT_ID, BOT_USERNAME),
    )

    assert telegram_bot.send_message.await_count == 1


async def test_a_late_yes_to_an_already_closed_session_says_so(db_pool, monkeypatch):
    """The worker's auto-close can beat a reply. Posting the summary anyway
    tells the person they closed it, which is not what happened."""
    active = await _new_active_session(db_pool)
    await db_pool.execute(
        "UPDATE sessions SET closing_question_asked_at = now() WHERE id = $1", active["id"]
    )
    active = await db_pool.fetchrow("SELECT * FROM sessions WHERE id = $1", active["id"])
    await session.close_session(db_pool, active["id"], reason="auto_close_silence")
    monkeypatch.setattr(router, "extract", AsyncMock(return_value={"reply": "yes"}))
    monkeypatch.setattr(router, "_summarize_session", AsyncMock(return_value="ИТОГИ"))
    telegram_bot = AsyncMock()

    await router.handle_active_message(
        db_pool, telegram_bot, active, _message("@orgbot да, всё"), BOT_ID, BOT_USERNAME
    )

    assert telegram_bot.send_message.await_args.kwargs["text"] == router._ALREADY_CLOSED
    row = await db_pool.fetchrow("SELECT closed_reason FROM sessions WHERE id = $1", active["id"])
    assert row["closed_reason"] == "auto_close_silence"


async def test_a_confirmed_action_that_did_nothing_is_not_reported_as_done(db_pool, monkeypatch):
    """R10 forbids the confident-but-wrong answer: if the item vanished between
    proposal and confirmation, "готово" claims a deletion that never happened."""
    active = await _new_active_session(db_pool)
    await core_tools.list_remove_item(db_pool, active["id"], "колбаса")
    monkeypatch.setattr(router, "extract", AsyncMock(return_value={"reply": "yes"}))
    telegram_bot = AsyncMock()

    await router.handle_active_message(
        db_pool, telegram_bot, active, _message("@orgbot да"), BOT_ID, BOT_USERNAME
    )

    assert telegram_bot.send_message.await_args.kwargs["text"] == router._CONFIRMED_NOTHING


async def test_a_participants_dm_reply_updates_their_status(db_pool, monkeypatch):
    """R2's third criterion. A DM has no session of its own, so without this the
    reply falls into the dormant path and tries to start a new session."""
    active = await _new_active_session(db_pool)
    await core_tools.set_participant(db_pool, active["id"], "Маша", "unknown", user_id=333)
    monkeypatch.setattr(router, "extract", AsyncMock(return_value={"reply": "yes"}))
    telegram_bot = AsyncMock()
    dm = Message(
        message_id=1, date=dt.datetime.now(dt.timezone.utc),
        chat=Chat(id=333, type="private"),
        from_user=User(id=333, first_name="Маша", is_bot=False), text="да, приду",
    )

    assert await router.handle_private_message(db_pool, telegram_bot, dm) is True

    rows = await db_pool.fetch("SELECT display_name, status FROM participants WHERE session_id = $1", active["id"])
    assert [(r["display_name"], r["status"]) for r in rows] == [("Маша", "confirmed")]


async def test_a_dm_from_someone_with_no_pending_nudge_falls_through(db_pool, monkeypatch):
    monkeypatch.setattr(router, "extract", AsyncMock(return_value={"reply": "yes"}))
    telegram_bot = AsyncMock()
    dm = Message(
        message_id=1, date=dt.datetime.now(dt.timezone.utc),
        chat=Chat(id=999, type="private"),
        from_user=User(id=999, first_name="Кто-то", is_bot=False), text="привет",
    )

    assert await router.handle_private_message(db_pool, telegram_bot, dm) is False
    telegram_bot.send_message.assert_not_awaited()


async def test_the_bot_says_the_ai_is_unreachable_rather_than_blaming_the_user(db_pool, monkeypatch):
    """When every model in the chain refuses, "не понял, переформулируй" is a
    lie: it sends the user off to retype a perfectly good message into a bot
    that cannot answer any message at all."""
    from bot.ai.client import AllModelsUnavailable

    active = await _new_active_session(db_pool)
    monkeypatch.setattr(router, "classify", AsyncMock(return_value=False))
    monkeypatch.setattr(router, "extract", AsyncMock(return_value={"reply": "unrelated"}))
    monkeypatch.setattr(
        router, "run_tool_loop", AsyncMock(side_effect=AllModelsUnavailable("all refused"))
    )
    telegram_bot = AsyncMock()

    await router.handle_active_message(
        db_pool, telegram_bot, active, _message("@orgbot что по списку?"), BOT_ID, BOT_USERNAME
    )

    sent = telegram_bot.send_message.await_args.kwargs["text"]
    assert sent in router._AI_UNAVAILABLE_MESSAGES
    assert sent != router._FALLBACK_MESSAGE


async def test_an_ordinary_tool_loop_failure_still_gets_the_generic_reply(db_pool, monkeypatch):
    """Only an exhausted chain gets the "come back later" line. A bug must not
    be reported to the group as a temporary outage."""
    active = await _new_active_session(db_pool)
    monkeypatch.setattr(router, "classify", AsyncMock(return_value=False))
    monkeypatch.setattr(router, "extract", AsyncMock(return_value={"reply": "unrelated"}))
    monkeypatch.setattr(router, "run_tool_loop", AsyncMock(side_effect=RuntimeError("boom")))
    telegram_bot = AsyncMock()

    await router.handle_active_message(
        db_pool, telegram_bot, active, _message("@orgbot что по списку?"), BOT_ID, BOT_USERNAME
    )

    assert telegram_bot.send_message.await_args.kwargs["text"] == router._FALLBACK_MESSAGE


def test_every_unavailable_message_offers_to_try_later():
    """Sarcasm is the tone, but the line still has to tell the user what to do."""
    for message in router._AI_UNAVAILABLE_MESSAGES:
        assert any(word in message.lower() for word in ("позже", "позднее", "через", "времени"))


async def test_the_bot_never_posts_the_words_it_was_told_to_answer_with(db_pool, monkeypatch):
    """Observed in a real chat: told to "respond with an empty string", the
    model wrote those two words into the group. A chat model cannot reliably
    emit nothing, so the instruction now asks for a sentinel — and every
    phrasing it reached for is treated as silence, because the model changing
    its mind about how to say "nothing" must not become a message."""
    active = await _new_active_session(db_pool)
    monkeypatch.setattr(router, "classify", AsyncMock(return_value=False))
    monkeypatch.setattr(router, "extract", AsyncMock(return_value={"reply": "unrelated"}))

    for said in ("empty string", "<silent>", "Empty String.", "пустая строка", ""):
        monkeypatch.setattr(router, "run_tool_loop", AsyncMock(return_value=said))
        telegram_bot = AsyncMock()

        await router.handle_active_message(
            db_pool, telegram_bot, active, _message("@orgbot записал"), BOT_ID, BOT_USERNAME
        )

        telegram_bot.send_message.assert_not_awaited()


async def test_a_real_answer_is_still_posted(db_pool, monkeypatch):
    """The silence guard must not swallow ordinary replies."""
    active = await _new_active_session(db_pool)
    monkeypatch.setattr(router, "classify", AsyncMock(return_value=False))
    monkeypatch.setattr(router, "extract", AsyncMock(return_value={"reply": "unrelated"}))
    monkeypatch.setattr(router, "run_tool_loop", AsyncMock(return_value="Готово, записал."))
    telegram_bot = AsyncMock()

    await router.handle_active_message(
        db_pool, telegram_bot, active, _message("@orgbot что там"), BOT_ID, BOT_USERNAME
    )

    assert telegram_bot.send_message.await_args.kwargs["text"] == "Готово, записал."


# --- S4: private replies (R5) ------------------------------------------------

async def test_private_reply_request_dms_the_asker_and_acks_in_group(db_pool, monkeypatch):
    """R5's first criterion: the answer arrives as a DM to the person who
    asked, and the group sees at most a short acknowledgement — proven by
    asserting the DM's chat_id is the asker's own id (_HUMAN, 7), not -100."""
    active = await _new_active_session(db_pool)
    telegram_bot = AsyncMock()
    monkeypatch.setattr(router, "classify", AsyncMock(return_value=False))
    private_text = "Вот список: помидоры, хлеб"

    async def fake_run_tool_loop(model_fn, text, registry, *, system_instruction):
        result = await registry["send_private_message"](text=private_text)
        assert result == {"status": "ok"}
        return "Отправил в личку."

    monkeypatch.setattr(router, "run_tool_loop", fake_run_tool_loop)
    msg = _message("пошли мне в личку список")

    await router.handle_active_message(db_pool, telegram_bot, active, msg, BOT_ID, BOT_USERNAME)

    telegram_bot.send_message.assert_any_await(chat_id=_HUMAN.id, text=private_text)
    telegram_bot.send_message.assert_any_await(chat_id=-100, text="Отправил в личку.")
    assert telegram_bot.send_message.await_count == 2


async def test_private_reply_cannot_reach_tells_group_to_start_a_chat_without_leaking_content(db_pool, monkeypatch):
    """R5's second criterion: a Forbidden DM must not be silently dropped, and
    the natural failure mode — pasting the content into the group instead —
    is exactly what the person was trying to avoid."""
    active = await _new_active_session(db_pool)
    telegram_bot = AsyncMock()
    telegram_bot.send_message = AsyncMock(side_effect=[Forbidden("bot was blocked by the user"), None])
    monkeypatch.setattr(router, "classify", AsyncMock(return_value=False))
    private_text = "Вот список: помидоры, хлеб, секретный ингредиент"

    async def fake_run_tool_loop(model_fn, text, registry, *, system_instruction):
        result = await registry["send_private_message"](text=private_text)
        assert result["status"] == "cannot_reach"
        return "Не получилось отправить в личку — откройте чат со мной и нажмите Start."

    monkeypatch.setattr(router, "run_tool_loop", fake_run_tool_loop)
    msg = _message("пошли мне в личку список")

    await router.handle_active_message(db_pool, telegram_bot, active, msg, BOT_ID, BOT_USERNAME)

    group_text = telegram_bot.send_message.await_args_list[-1].kwargs["text"]
    assert telegram_bot.send_message.await_args_list[-1].kwargs["chat_id"] == -100
    assert "start" in group_text.lower()
    assert private_text not in group_text


async def test_private_reply_with_no_from_user_does_not_crash(db_pool, monkeypatch):
    """A channel post has no from_user — the router must bind current_user_id
    as None rather than crash, and the tool must fail gracefully rather than
    attempting a send with no destination."""
    active = await _new_active_session(db_pool)
    telegram_bot = AsyncMock()
    monkeypatch.setattr(router, "classify", AsyncMock(return_value=False))

    async def fake_run_tool_loop(model_fn, text, registry, *, system_instruction):
        result = await registry["send_private_message"](text="что угодно")
        assert result == {"status": "failed", "detail": "no telegram user to message"}
        return "Не получилось понять, кому писать в личку."

    monkeypatch.setattr(router, "run_tool_loop", fake_run_tool_loop)
    handle = f"@{BOT_USERNAME}"
    text = f"{handle} пошли в личку список"
    msg = Message(
        message_id=1, date=dt.datetime.now(dt.timezone.utc),
        chat=Chat(id=-100, type="channel"), from_user=None, text=text,
        entities=[MessageEntity(type=MessageEntity.MENTION, offset=0, length=len(handle))],
    )

    await router.handle_active_message(db_pool, telegram_bot, active, msg, BOT_ID, BOT_USERNAME)

    telegram_bot.send_message.assert_awaited_once_with(
        chat_id=-100, text="Не получилось понять, кому писать в личку."
    )


def test_send_private_message_is_session_bound():
    assert "send_private_message" in router._SESSION_BOUND_TOOLS


def test_the_system_instruction_covers_private_replies():
    """R5. The instruction has to name send_private_message directly, cover
    the cannot_reach failure with what to tell the group, and explicitly
    forbid pasting the private content into the group — the natural model
    behaviour on a failed DM, and precisely what the person asked to avoid."""
    instruction = router._ACTIVE_MODE_SYSTEM_INSTRUCTION

    assert "send_private_message" in instruction
    assert "cannot_reach" in instruction
    assert "Start" in instruction
    assert "do not repeat the private content" in instruction.lower()


def test_the_system_instruction_demands_russian_without_transliterating_names():
    """R6. The second sentence isn't decoration: 0001's S6 already had a model
    translate "огурцы" to "cucumbers" and check off a second copy — R4 makes
    item names a database key, so telling the model to answer in Russian
    without the no-transliteration caveat invites that bug straight back."""
    instruction = router._ACTIVE_MODE_SYSTEM_INSTRUCTION.lower()

    assert "russian" in instruction
    assert "transliterat" in instruction


def test_the_system_instruction_covers_repeating_reminders_and_the_schedule_view():
    """R1/R2. The `bugs` complaint was the bot saying it had no way to check
    what was scheduled — the instruction has to point at reminder_list now
    that one exists, and at repeat_until so the model asks how long to keep
    reminding instead of guessing."""
    instruction = router._ACTIVE_MODE_SYSTEM_INSTRUCTION.lower()

    assert "reminder_list" in instruction
    assert "repeat_until" in instruction


def test_the_system_instruction_covers_quantity_ownership_and_categories():
    """R4. list_claim/list_unclaim have to be named directly, not just left
    for the model to discover in the tool schema, and the category vocabulary
    has to be spelled out: a model asked to invent one will say "молочка"
    once and "молочные продукты" the next, which sorts differently in
    bot.list_render and looks broken."""
    from bot.list_render import LIST_CATEGORIES

    instruction = router._ACTIVE_MODE_SYSTEM_INSTRUCTION.lower()

    assert "list_claim" in instruction
    assert "list_unclaim" in instruction
    for category in LIST_CATEGORIES:
        assert category in instruction


def test_the_system_instruction_forbids_inventing_an_amount():
    instruction = router._ACTIVE_MODE_SYSTEM_INSTRUCTION.lower()

    assert "never invent an amount" in instruction


def test_list_claim_and_unclaim_are_session_bound():
    """Session-bound tools have their session_id enforced by the router
    rather than trusted from the model (see _bind_session_context) — the same
    reason every other session-scoped tool is in this set."""
    assert "list_claim" in router._SESSION_BOUND_TOOLS
    assert "list_unclaim" in router._SESSION_BOUND_TOOLS


async def test_the_model_is_told_what_day_it_is(db_pool):
    """Observed in a real chat: asked to remind "через 5 минут", the model
    stored 2025-07-20 — over a year in the past — because nothing told it the
    date and it used whatever its training data suggested. It only looked like
    it worked because an overdue reminder fires on the next poll."""
    import datetime as dt

    active = await _new_active_session(db_pool)
    await db_pool.execute("UPDATE chats SET timezone = 'Europe/Moscow' WHERE chat_id = $1", active["chat_id"])

    instruction = await router._active_mode_instruction(
        db_pool, active["chat_id"], active["id"], _HUMAN
    )

    today = dt.datetime.now(ZoneInfo("Europe/Moscow"))
    assert today.strftime("%Y-%m-%d") in instruction
    assert "Europe/Moscow" in instruction
    assert "reminder_set" in instruction


async def test_the_date_follows_the_chats_own_timezone(db_pool):
    """A chat on the other side of the date line must not be told the server's
    day."""
    import datetime as dt

    active = await _new_active_session(db_pool)
    await db_pool.execute(
        "UPDATE chats SET timezone = 'Pacific/Kiritimati' WHERE chat_id = $1", active["chat_id"]
    )

    instruction = await router._active_mode_instruction(
        db_pool, active["chat_id"], active["id"], _HUMAN
    )

    local = dt.datetime.now(ZoneInfo("Pacific/Kiritimati"))
    assert local.strftime("%Y-%m-%d") in instruction


def test_the_system_instruction_covers_the_roster_gap_remark():
    """R7's last three criteria: the roster is partial by construction, so a
    list of three names in a group of nine is misleading on its own and the
    person asking cannot tell the difference. The instruction has to name the
    exact fields and the exact condition — only when they differ, never when
    the count is unknown, or a stray 0 would read as an empty group."""
    instruction = router._ACTIVE_MODE_SYSTEM_INSTRUCTION

    assert "chat_member_count" in instruction
    assert "recorded_count" in instruction
    assert "В чате" in instruction
    assert "null" in instruction.lower()
