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

import bot.farewells as farewells
import bot.router as router
import bot.session as session
import bot.tools.core as core_tools
import bot.settings as settings

BOT_ID = 4242
BOT_USERNAME = "orgbot"

_BOT = User(id=BOT_ID, first_name="Organizer", is_bot=True, username=BOT_USERNAME)
_HUMAN = User(id=7, first_name="Sasha", is_bot=False, username="sashahandle")


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
        # The sender's own handle, which the model has no reliable way to
        # supply: it sees the name, but only this binding knows the id and
        # the handle belong to one person.
        "username": "sashahandle",
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


async def test_bind_session_context_forces_current_user_id_for_set_timezone():
    """whose='me' relocates a person. If the model chose the id it could move
    a bystander to another continent and every reminder addressed to them
    with it — the same reasoning that keeps the recipient off
    send_private_message."""
    seen = {}

    async def set_timezone(**kwargs):
        seen.update(kwargs)
        return {"status": "ok"}

    registry = router._bind_session_context({"set_timezone": set_timezone}, 1, _HUMAN)

    await registry["set_timezone"](timezone_name="Europe/Moscow", whose="me", current_user_id=999)

    assert seen["current_user_id"] == 7


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


async def test_the_chat_title_never_reaches_the_start_classifier(db_pool, monkeypatch):
    """It used to be handed over as context and it decided the outcome: «Море
    3/9» saying "начинай это отслеживать" was refused for naming no activity,
    while the same words in «Пикник на море 3/9» started a session. What a
    group calls itself is neither consent nor a description of the event."""
    await _ensure_chat(db_pool, title="Пикник 15 сентября")
    telegram_bot = AsyncMock()
    extract_mock = AsyncMock(return_value={
        "confident": True, "activity_type": "picnic",
        "event_date": "2026-09-15", "event_date_raw": "15 сентября",
    })
    monkeypatch.setattr(router, "extract", extract_mock)
    msg = _message("следи за пикником", chat_title="Пикник 15 сентября")

    await router.handle_dormant_message(db_pool, telegram_bot, msg, BOT_ID, BOT_USERNAME)

    call_text = extract_mock.await_args.args[1]
    assert "Пикник 15 сентября" not in call_text
    assert call_text == "Message: @orgbot следи за пикником"

    # A date the *message* states is still taken.
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


async def test_joining_clears_any_per_chat_command_menu(db_pool, monkeypatch):
    """A chat-level scope set to an empty list swallows the slash menu while
    the commands keep working, and getMyCommands cannot tell it from a scope
    that was never set — so it is deleted on the way in rather than hunted
    down later. Live, three groups had exactly that and showed no menu."""
    from telegram import BotCommandScopeChat, BotCommandScopeChatAdministrators

    _no_group_info(monkeypatch)
    telegram_bot = AsyncMock()

    await router.handle_bot_added(
        db_pool, telegram_bot, _member_update("left", "member"), BOT_ID, BOT_USERNAME
    )

    cleared = {type(c.kwargs["scope"]): c.kwargs["scope"].chat_id
               for c in telegram_bot.delete_my_commands.await_args_list}
    assert cleared == {BotCommandScopeChat: -100,
                       BotCommandScopeChatAdministrators: -100}


async def test_a_promotion_clears_nothing(db_pool):
    """Being promoted is not joining — the menu was already right."""
    telegram_bot = AsyncMock()

    await router.handle_bot_added(
        db_pool, telegram_bot, _member_update("member", "administrator"),
        BOT_ID, BOT_USERNAME,
    )

    telegram_bot.delete_my_commands.assert_not_awaited()


async def test_the_greeting_survives_a_failed_scope_clear(db_pool, monkeypatch):
    """Refusing to greet a group because a cosmetic call was rate-limited is
    worse than a group with no menu."""
    _no_group_info(monkeypatch)
    telegram_bot = AsyncMock()
    telegram_bot.delete_my_commands = AsyncMock(side_effect=Exception("flood wait"))

    await router.handle_bot_added(
        db_pool, telegram_bot, _member_update("left", "member"), BOT_ID, BOT_USERNAME
    )

    telegram_bot.send_message.assert_awaited_once()


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


async def test_the_greeting_reads_the_title_back(db_pool, monkeypatch):
    """Reporting is not deciding. The title was taken out of the *start*
    decision and stays out; what went with it by mistake was this — saying
    out loud what can be seen, before anything is tracked."""
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

    await router.handle_bot_added(
        db_pool, telegram_bot, _member_update("left", "member"), BOT_ID, BOT_USERNAME
    )

    text = telegram_bot.send_message.await_args.kwargs["text"]
    assert "поход" in text
    assert "15.09.2026" in text
    assert "поляна Ханания" in text
    assert "9 человек" in text
    # Read back, not started.
    assert await session.get_active_session(db_pool, -100) is None


async def test_the_greeting_says_nothing_it_did_not_find(db_pool, monkeypatch):
    """R7's last criterion: a title with nothing in it must not scaffold an
    empty «Поездка: не указано»."""
    _no_group_info(monkeypatch)
    telegram_bot = AsyncMock()

    await router.handle_bot_added(
        db_pool, telegram_bot, _member_update("left", "member"), BOT_ID, BOT_USERNAME
    )

    telegram_bot.send_message.assert_awaited_once_with(
        chat_id=-100, text=router._GREETING_TEMPLATE.format(mention=f"@{BOT_USERNAME}")
    )


async def test_the_greeting_asks_whether_to_start(db_pool, monkeypatch):
    """One question. Being added is still not consent — nothing is tracked
    until somebody answers."""
    _no_group_info(monkeypatch)
    telegram_bot = AsyncMock()

    await router.handle_bot_added(
        db_pool, telegram_bot, _member_update("left", "member"), BOT_ID, BOT_USERNAME
    )

    text = telegram_bot.send_message.await_args.kwargs["text"]
    assert "?" in text and f"@{BOT_USERNAME}" in text
    assert await session.get_active_session(db_pool, -100) is None


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
    monkeypatch.setattr(router, "_addressed_intent", AsyncMock(return_value={"stop": True, "off_topic": False}))
    msg = _message("всё, спасибо, свободен")

    await router.handle_active_message(db_pool, telegram_bot, active, msg, BOT_ID, BOT_USERNAME)

    row = await db_pool.fetchrow("SELECT status, closed_reason FROM sessions WHERE id = $1", active["id"])
    assert (row["status"], row["closed_reason"]) == ("closed", "explicit_stop")
    telegram_bot.send_message.assert_awaited_once()
    assert telegram_bot.send_message.await_args.kwargs["chat_id"] == -100
    # Membership, not a prefix: the sign-off is one phrase from the list, and
    # a startswith check would pass on a message that glued two together.
    assert telegram_bot.send_message.await_args.kwargs["text"] in farewells.FAREWELLS
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
    monkeypatch.setattr(router, "_addressed_intent", AsyncMock(return_value={"stop": True, "off_topic": False}))
    msg = _message("больше не нужен, можешь отдыхать")

    await router.handle_active_message(db_pool, telegram_bot, active, msg, BOT_ID, BOT_USERNAME)

    row = await db_pool.fetchrow("SELECT status, closed_reason FROM sessions WHERE id = $1", active["id"])
    assert (row["status"], row["closed_reason"]) == ("closed", "explicit_stop")


async def test_various_stop_phrasings_all_close_via_the_classifier(db_pool, monkeypatch):
    telegram_bot = AsyncMock()
    monkeypatch.setattr(router, "_addressed_intent", AsyncMock(return_value={"stop": True, "off_topic": False}))

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
    monkeypatch.setattr(router, "_addressed_intent", AsyncMock(return_value={"stop": False, "off_topic": False}))
    monkeypatch.setattr(router, "run_tool_loop", AsyncMock(return_value="Вот список: помидоры"))
    msg = _message("что у нас в списке")

    await router.handle_active_message(db_pool, telegram_bot, active, msg, BOT_ID, BOT_USERNAME)

    telegram_bot.send_message.assert_awaited_once_with(chat_id=-100, text="Вот список: помидоры")
    assert await _decision_log_count(db_pool) == 1


async def test_markdown_in_tool_loop_reply_is_converted_before_sending(db_pool, monkeypatch):
    active = await _new_active_session(db_pool)
    telegram_bot = AsyncMock()
    monkeypatch.setattr(router, "_addressed_intent", AsyncMock(return_value={"stop": False, "off_topic": False}))
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
    monkeypatch.setattr(router, "_addressed_intent", AsyncMock(return_value={"stop": False, "off_topic": False}))
    monkeypatch.setattr(router, "run_tool_loop", AsyncMock(return_value="**<silent>**"))
    msg = _message("записал")

    await router.handle_active_message(db_pool, telegram_bot, active, msg, BOT_ID, BOT_USERNAME)

    telegram_bot.send_message.assert_not_awaited()


async def test_a_reply_with_no_markdown_passes_through_unchanged(db_pool, monkeypatch):
    active = await _new_active_session(db_pool)
    telegram_bot = AsyncMock()
    monkeypatch.setattr(router, "_addressed_intent", AsyncMock(return_value={"stop": False, "off_topic": False}))
    monkeypatch.setattr(router, "run_tool_loop", AsyncMock(return_value="Готово, записал."))
    msg = _message("ладно")

    await router.handle_active_message(db_pool, telegram_bot, active, msg, BOT_ID, BOT_USERNAME)

    telegram_bot.send_message.assert_awaited_once_with(chat_id=-100, text="Готово, записал.")


async def test_empty_tool_loop_reply_posts_nothing(db_pool, monkeypatch):
    active = await _new_active_session(db_pool)
    telegram_bot = AsyncMock()
    monkeypatch.setattr(router, "_addressed_intent", AsyncMock(return_value={"stop": False, "off_topic": False}))
    monkeypatch.setattr(router, "run_tool_loop", AsyncMock(return_value="   "))
    msg = _message("ладно проехали")

    await router.handle_active_message(db_pool, telegram_bot, active, msg, BOT_ID, BOT_USERNAME)

    telegram_bot.send_message.assert_not_awaited()
    assert await _decision_log_count(db_pool) == 1


async def test_tool_loop_failure_sends_fixed_fallback_message(db_pool, monkeypatch):
    active = await _new_active_session(db_pool)
    telegram_bot = AsyncMock()
    monkeypatch.setattr(router, "_addressed_intent", AsyncMock(return_value={"stop": False, "off_topic": False}))
    monkeypatch.setattr(router, "run_tool_loop", AsyncMock(side_effect=RuntimeError("boom")))
    msg = _message("???")

    await router.handle_active_message(db_pool, telegram_bot, active, msg, BOT_ID, BOT_USERNAME)

    telegram_bot.send_message.assert_awaited_once_with(chat_id=-100, text=router._FALLBACK_MESSAGE)


async def test_two_simultaneous_stops_post_one_summary(db_pool, monkeypatch):
    """close_session returns False for whoever lost the race, precisely so the
    loser doesn't post a second closing summary."""
    import asyncio

    active = await _new_active_session(db_pool)
    monkeypatch.setattr(router, "_addressed_intent", AsyncMock(return_value={"stop": True, "off_topic": False}))
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
    monkeypatch.setattr(router, "_addressed_intent", AsyncMock(return_value={"stop": False, "off_topic": False}))
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
    monkeypatch.setattr(router, "_addressed_intent", AsyncMock(return_value={"stop": False, "off_topic": False}))
    monkeypatch.setattr(router, "extract", AsyncMock(return_value={"reply": "unrelated"}))
    monkeypatch.setattr(router, "run_tool_loop", AsyncMock(side_effect=RuntimeError("boom")))
    telegram_bot = AsyncMock()

    await router.handle_active_message(
        db_pool, telegram_bot, active, _message("@orgbot что по списку?"), BOT_ID, BOT_USERNAME
    )

    assert telegram_bot.send_message.await_args.kwargs["text"] == router._FALLBACK_MESSAGE


def test_every_unavailable_message_offers_to_try_later():
    """Sarcasm is the tone, but the line still has to tell the user what to
    do. On its own the joke leaves nobody knowing whether to wait or give
    up."""
    for message in router._AI_UNAVAILABLE_MESSAGES:
        assert any(word in message.lower() for word in ("позже", "позднее", "через", "времени"))


def test_the_unavailable_line_is_the_groups_own_words():
    """Asked for by the user, verbatim. A bot that says the same thing every
    time it breaks is easier to recognise than one that is inventive about
    it — which is why this is one line and not a rotating set."""
    assert router._AI_UNAVAILABLE_MESSAGES == ("Мне временно снесло крышу. Попробуйте позже.",)


async def test_the_bot_never_posts_the_words_it_was_told_to_answer_with(db_pool, monkeypatch):
    """Observed in a real chat: told to "respond with an empty string", the
    model wrote those two words into the group. A chat model cannot reliably
    emit nothing, so the instruction now asks for a sentinel — and every
    phrasing it reached for is treated as silence, because the model changing
    its mind about how to say "nothing" must not become a message."""
    active = await _new_active_session(db_pool)
    monkeypatch.setattr(router, "_addressed_intent", AsyncMock(return_value={"stop": False, "off_topic": False}))
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
    monkeypatch.setattr(router, "_addressed_intent", AsyncMock(return_value={"stop": False, "off_topic": False}))
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
    monkeypatch.setattr(router, "_addressed_intent", AsyncMock(return_value={"stop": False, "off_topic": False}))
    private_text = "Вот список: помидоры, хлеб"

    # **_ rather than a spelled-out signature: run_tool_loop has gained a
    # keyword argument twice now, and each time these doubles raised
    # TypeError that the router caught and turned into its generic reply —
    # so a signature mismatch looked like a wrong answer, not a crash.
    async def fake_run_tool_loop(model_fn, text, registry, *, system_instruction, **_):
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
    monkeypatch.setattr(router, "_addressed_intent", AsyncMock(return_value={"stop": False, "off_topic": False}))
    private_text = "Вот список: помидоры, хлеб, секретный ингредиент"

    async def fake_run_tool_loop(model_fn, text, registry, *, system_instruction, **_):
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
    monkeypatch.setattr(router, "_addressed_intent", AsyncMock(return_value={"stop": False, "off_topic": False}))

    async def fake_run_tool_loop(model_fn, text, registry, *, system_instruction, **_):
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


async def test_silent_capture_skips_a_name_that_looks_like_a_participant(db_pool, monkeypatch):
    """Reproduced live: an unaddressed message naming who is coming let the
    silent-capture classifier confuse a person with something to bring, and
    list_add stored the participant's own name as a bare shopping-list item.
    This path never reacts to a tool result — it just applies whatever came
    back — so the guard inside list_add itself is what keeps the list clean
    here; nothing else on this path inspects the return value."""
    import bot.tools.core as core_tools

    active = await _new_active_session(db_pool)
    await core_tools.set_participant(db_pool, active["id"], "Андрюха", "confirmed", user_id=1)
    monkeypatch.setattr(
        router, "extract",
        AsyncMock(return_value={"list_items": ["Андрюха"], "checked_off_items": [], "facts": []}),
    )
    telegram_bot = AsyncMock()

    await router.handle_active_message(
        db_pool, telegram_bot, active, _message("андрюха тоже придёт", addressed=False), BOT_ID, BOT_USERNAME
    )

    assert (await core_tools.list_show(db_pool, active["id"]))["items"] == []
    telegram_bot.send_message.assert_not_awaited()


async def test_the_tool_loop_is_given_the_recent_conversation(db_pool):
    """The router is where history reaches the model. Live, it passed none:
    the bot asked for the whole new amount, the person replied "1 литр", and
    it answered "Что именно 1 литр?" — with no memory of having asked."""
    from unittest.mock import AsyncMock, patch

    import bot.decision_log as decision_log

    chat_id = -9001
    active = await _new_active_session(db_pool, chat_id=chat_id)
    await decision_log.log_decision(
        db_pool, chat_id=chat_id, user_id=1, raw_text="Добавь ещё 0.5 воды",
        stage="tool_call", decision={"session_id": active["id"], "reply": "Сколько всего?"},
    )
    telegram_bot = AsyncMock()

    with patch("bot.router.run_tool_loop", AsyncMock(return_value="ок")) as loop, \
            patch("bot.router._addressed_intent", AsyncMock(return_value={"stop": False, "off_topic": False})):
        await router.handle_active_message(
            db_pool, telegram_bot, active,
            _message("@bot 1 литр", chat_id=chat_id), BOT_ID, BOT_USERNAME,
        )

    passed = loop.await_args.kwargs["history"]
    assert {"role": "user", "content": "Добавь ещё 0.5 воды"} in passed
    assert {"role": "assistant", "content": "Сколько всего?"} in passed


async def test_a_grant_is_issued_for_the_turn_and_revoked_after(db_pool, monkeypatch):
    """A CLI-backed model reaches for tools itself, so it needs somewhere to
    call and something that says which session it is calling as. The token
    should be valid for about as long as it is needed, no longer."""
    from unittest.mock import AsyncMock, patch

    import bot.mcp_server as mcp_server

    grants = mcp_server.GrantStore()
    monkeypatch.setattr(router, "MCP_BASE_URL", "http://bot-organizer-bot:8081")
    router.set_grant_store(grants)
    active = await _new_active_session(db_pool)
    telegram_bot = AsyncMock()
    seen = {}

    async def fake_run_tool_loop(model_fn, text, registry, *, system_instruction, history=None, mcp_url=None, record=None):
        seen["url"] = mcp_url
        seen["resolved"] = grants.resolve(mcp_url.rsplit("/", 1)[-1])
        return "ок"

    monkeypatch.setattr(router, "run_tool_loop", fake_run_tool_loop)
    monkeypatch.setattr(router, "_addressed_intent", AsyncMock(return_value={"stop": False, "off_topic": False}))

    await router.handle_active_message(
        db_pool, telegram_bot, active, _message("@bot покажи список"), BOT_ID, BOT_USERNAME,
    )
    router.set_grant_store(None)

    assert seen["url"].startswith("http://bot-organizer-bot:8081/mcp/")
    assert seen["resolved"].session_id == active["id"], "the grant must name this session"
    token = seen["url"].rsplit("/", 1)[-1]
    assert grants.resolve(token) is None, "the token must not outlive the turn"


async def test_no_grant_and_no_url_when_nothing_serves_mcp(db_pool, monkeypatch):
    """Every deployment that has not enabled it. Sending an address nothing
    answers on would be worse than sending none."""
    from unittest.mock import AsyncMock

    import bot.mcp_server as mcp_server

    # A store *is* present — otherwise this passes whether or not the
    # MCP_BASE_URL guard exists, which is how it passed a sabotage run that
    # removed the guard entirely.
    class _CountingStore(mcp_server.GrantStore):
        """Counts issues. Checking the store is empty afterwards cannot fail:
        the router revokes in a finally, so it is empty either way."""

        issued = 0

        def issue(self, *a, **kw):
            type(self).issued += 1
            return super().issue(*a, **kw)

    grants = _CountingStore()
    router.set_grant_store(grants)
    monkeypatch.setattr(router, "MCP_BASE_URL", None)
    active = await _new_active_session(db_pool)
    seen = {}

    async def fake_run_tool_loop(model_fn, text, registry, *, system_instruction, history=None, mcp_url=None, record=None):
        seen["url"] = mcp_url
        return "ок"

    monkeypatch.setattr(router, "run_tool_loop", fake_run_tool_loop)
    monkeypatch.setattr(router, "_addressed_intent", AsyncMock(return_value={"stop": False, "off_topic": False}))

    await router.handle_active_message(
        db_pool, AsyncMock(), active, _message("@bot покажи список"), BOT_ID, BOT_USERNAME,
    )

    router.set_grant_store(None)

    # Both halves: no address to send, and no token minted to go in one.
    assert seen["url"] is None
    assert type(grants).issued == 0, "a token was issued with nowhere to use it"


async def test_the_grant_is_revoked_even_when_the_turn_fails(db_pool, monkeypatch):
    """The TTL is a backstop, not the plan."""
    from unittest.mock import AsyncMock

    import bot.mcp_server as mcp_server

    grants = mcp_server.GrantStore()
    monkeypatch.setattr(router, "MCP_BASE_URL", "http://bot:8081")
    router.set_grant_store(grants)
    active = await _new_active_session(db_pool)
    tokens = []

    async def exploding_loop(model_fn, text, registry, *, system_instruction, history=None, mcp_url=None, record=None):
        tokens.append(mcp_url.rsplit("/", 1)[-1])
        raise RuntimeError("the model fell over")

    monkeypatch.setattr(router, "run_tool_loop", exploding_loop)
    monkeypatch.setattr(router, "_addressed_intent", AsyncMock(return_value={"stop": False, "off_topic": False}))

    await router.handle_active_message(
        db_pool, AsyncMock(), active, _message("@bot покажи список"), BOT_ID, BOT_USERNAME,
    )
    router.set_grant_store(None)

    assert grants.resolve(tokens[0]) is None


# --- staying on the event ----------------------------------------------------

async def test_an_off_topic_request_is_turned_away_without_a_model_turn(db_pool, monkeypatch):
    """The bot answered "дай рецепт пасты" in a group organizing a picnic.
    The refusal has to happen before the tool loop, not inside the reply: a
    turn that reaches the model has already been paid for and can still be
    talked into answering."""
    active = await _new_active_session(db_pool)
    monkeypatch.setattr(
        router, "_addressed_intent", AsyncMock(return_value={"stop": False, "off_topic": True})
    )
    loop = AsyncMock(return_value="рецепт пасты")
    monkeypatch.setattr(router, "run_tool_loop", loop)
    telegram_bot = AsyncMock()

    await router.handle_active_message(
        db_pool, telegram_bot, active, _message("@orgbot дай рецепт пасты"), BOT_ID, BOT_USERNAME
    )

    loop.assert_not_awaited()
    telegram_bot.send_message.assert_awaited_once_with(
        chat_id=active["chat_id"], text=router._OFF_TOPIC_REPLY
    )


async def test_the_senders_identity_is_folded_onto_their_roster_row(db_pool, monkeypatch):
    """A person's own message is the only place Telegram hands over their id,
    their handle and their name together. Miss it and the row someone else
    wrote about them by @handle never heals."""
    active = await _new_active_session(db_pool, chat_id=-140)
    await core_tools.set_participant(db_pool, active["id"], "@sashahandle", "declined")
    await core_tools.set_participant(db_pool, active["id"], "Sasha", "unknown", user_id=7)
    monkeypatch.setattr(
        router, "_addressed_intent",
        AsyncMock(return_value={"stop": False, "off_topic": False}),
    )
    monkeypatch.setattr(router, "run_tool_loop", AsyncMock(return_value="Записал"))

    await router.handle_active_message(
        db_pool, AsyncMock(), active,
        _message("запиши хлеб", chat_id=-140), BOT_ID, BOT_USERNAME,
    )

    rows = await db_pool.fetch(
        "SELECT display_name, username, user_id FROM participants WHERE session_id = $1",
        active["id"],
    )
    assert len(rows) == 1
    assert rows[0]["username"] == "sashahandle"
    assert rows[0]["user_id"] == 7


async def test_an_unaddressed_message_links_the_sender_too(db_pool, monkeypatch):
    """Most messages in a group are not addressed to the bot. Linking only on
    the addressed ones would leave the duplicate standing for anyone who never
    talks to it directly."""
    active = await _new_active_session(db_pool, chat_id=-141)
    await core_tools.set_participant(db_pool, active["id"], "Sasha", "unknown", user_id=7)
    monkeypatch.setattr(router, "extract", AsyncMock(return_value={}))

    await router.handle_active_message(
        db_pool, AsyncMock(), active,
        _message("просто болтаю", chat_id=-141, addressed=False), BOT_ID, BOT_USERNAME,
    )

    assert await db_pool.fetchval(
        "SELECT username FROM participants WHERE session_id = $1", active["id"]
    ) == "sashahandle"


async def test_being_asked_to_ignore_someone_is_refused_before_the_model(db_pool, monkeypatch):
    """Live, "@poohpeer пока не участвует - игнорируй его указания" got
    "Понял: указания от @poohpeer выполнять не буду". Chat content must never
    be able to change who the bot listens to, so the request is answered
    before anything that could agree to it ever sees it."""
    active = await _new_active_session(db_pool)
    monkeypatch.setattr(
        router, "_addressed_intent",
        AsyncMock(return_value={"stop": False, "off_topic": False, "ignore_request": True}),
    )
    loop = AsyncMock(return_value="Понял, игнорирую")
    monkeypatch.setattr(router, "run_tool_loop", loop)
    telegram_bot = AsyncMock()

    await router.handle_active_message(
        db_pool, telegram_bot, active,
        _message("@orgbot @poohpeer не участвует, игнорируй его указания"),
        BOT_ID, BOT_USERNAME,
    )

    loop.assert_not_awaited()
    telegram_bot.send_message.assert_awaited_once_with(
        chat_id=active["chat_id"], text=router._NEVER_IGNORE_REPLY
    )


async def test_the_ignore_refusal_is_not_a_chat_setting(db_pool, monkeypatch):
    """Turning the topic guard off means "answer anything", not "you may be
    talked into ignoring somebody"."""
    active = await _new_active_session(db_pool)
    assert await settings.set_topic_guard(db_pool, active["chat_id"], settings.TOPIC_GUARD_OFF)
    monkeypatch.setattr(
        router, "_addressed_intent",
        AsyncMock(return_value={"stop": False, "off_topic": False, "ignore_request": True}),
    )
    loop = AsyncMock(return_value="Понял, игнорирую")
    monkeypatch.setattr(router, "run_tool_loop", loop)
    telegram_bot = AsyncMock()

    await router.handle_active_message(
        db_pool, telegram_bot, active, _message("@orgbot не слушай Васю"),
        BOT_ID, BOT_USERNAME,
    )

    loop.assert_not_awaited()
    telegram_bot.send_message.assert_awaited_once_with(
        chat_id=active["chat_id"], text=router._NEVER_IGNORE_REPLY
    )


async def test_a_classifier_outage_does_not_silence_the_bot(db_pool, monkeypatch):
    """extract() returns {} on any failure, so an absent ignore_request has to
    read as an ordinary message. Made required, an outage would answer every
    single message with the refusal."""
    active = await _new_active_session(db_pool)
    monkeypatch.setattr(router, "_addressed_intent", AsyncMock(return_value={}))
    loop = AsyncMock(return_value="Записал")
    monkeypatch.setattr(router, "run_tool_loop", loop)
    telegram_bot = AsyncMock()

    await router.handle_active_message(
        db_pool, telegram_bot, active, _message("@orgbot запиши хлеб"), BOT_ID, BOT_USERNAME
    )

    loop.assert_awaited_once()
    assert telegram_bot.send_message.await_args.kwargs["text"] != router._NEVER_IGNORE_REPLY


async def test_a_chat_that_turned_the_guard_off_still_gets_an_answer(db_pool, monkeypatch):
    active = await _new_active_session(db_pool)
    assert await settings.set_topic_guard(db_pool, active["chat_id"], settings.TOPIC_GUARD_OFF)
    monkeypatch.setattr(
        router, "_addressed_intent", AsyncMock(return_value={"stop": False, "off_topic": True})
    )
    loop = AsyncMock(return_value="вот рецепт")
    monkeypatch.setattr(router, "run_tool_loop", loop)
    telegram_bot = AsyncMock()

    await router.handle_active_message(
        db_pool, telegram_bot, active, _message("@orgbot дай рецепт пасты"), BOT_ID, BOT_USERNAME
    )

    loop.assert_awaited_once()


async def test_stop_is_decided_before_off_topic(db_pool, monkeypatch):
    """"спасибо, всё" is not about the event either. Judged off-topic first,
    it would be refused instead of closing the session, and the only way to
    dismiss the bot would stop working."""
    active = await _new_active_session(db_pool)
    monkeypatch.setattr(
        router, "_addressed_intent", AsyncMock(return_value={"stop": True, "off_topic": True})
    )
    telegram_bot = AsyncMock()

    await router.handle_active_message(
        db_pool, telegram_bot, active, _message("@orgbot спасибо, всё"), BOT_ID, BOT_USERNAME
    )

    assert telegram_bot.send_message.await_args.kwargs["text"] in farewells.FAREWELLS
    row = await db_pool.fetchrow("SELECT status FROM sessions WHERE id = $1", active["id"])
    assert row["status"] == "closed"


async def test_a_classifier_that_answered_nothing_lets_the_message_through(db_pool, monkeypatch):
    """extract() returns {} on any failure. The guard is phrased so that reads
    as "not off-topic": a classifier outage must leave the bot working, not
    have it refuse every message in every chat."""
    active = await _new_active_session(db_pool)
    monkeypatch.setattr(router, "extract", AsyncMock(return_value={}))
    loop = AsyncMock(return_value="ок")
    monkeypatch.setattr(router, "run_tool_loop", loop)
    telegram_bot = AsyncMock()

    await router.handle_active_message(
        db_pool, telegram_bot, active, _message("@orgbot сколько хлеба?"), BOT_ID, BOT_USERNAME
    )

    loop.assert_awaited_once()


async def test_the_classifier_is_told_what_the_bot_just_asked(db_pool):
    """A bare "5" answering the bot's own question is not a new subject. With
    no previous message in front of it the classifier sees a contentless
    fragment, and the guard would refuse the answer to a question the bot
    itself had asked."""
    turns = [
        {"role": "user", "content": "Добавь воды"},
        {"role": "assistant", "content": "Сколько всего?"},
    ]

    prompt = router._intent_input("5", turns, "пикник")

    assert "Сколько всего?" in prompt
    assert prompt.endswith("Message: 5")


def test_the_prompt_holds_no_stale_question_when_the_bot_has_not_spoken():
    assert router._intent_input("привет", [], "пикник") == "Currently tracking: пикник\n\nMessage: привет"


async def test_closing_says_one_line_and_no_shopping_list(db_pool, monkeypatch):
    """The summary read the list back at the one moment nobody needs it — a
    wall of text about an event that had just finished."""
    active = await _new_active_session(db_pool)
    await core_tools.list_add(db_pool, active["id"], "мангал")
    await core_tools.list_add(db_pool, active["id"], "уголь")
    monkeypatch.setattr(
        router, "_addressed_intent", AsyncMock(return_value={"stop": True, "off_topic": False})
    )
    telegram_bot = AsyncMock()

    await router.handle_active_message(
        db_pool, telegram_bot, active, _message("@orgbot всё, спасибо"), BOT_ID, BOT_USERNAME
    )

    said = telegram_bot.send_message.await_args.kwargs["text"]
    assert said in farewells.FAREWELLS
    assert "мангал" not in said and "уголь" not in said


# --- one event per group -----------------------------------------------------

def _intent(**over):
    base = {"stop": False, "off_topic": False, "new_event": False}
    base.update(over)
    return base


async def test_a_second_event_is_refused_and_a_switch_offered(db_pool, monkeypatch):
    """A group gets one session. Quietly ignoring the proposal, or quietly
    switching to it, both leave the group unsure which event the bot is on."""
    active = await _new_active_session(db_pool, activity_type="пикник в парке")
    monkeypatch.setattr(router, "_addressed_intent", AsyncMock(
        return_value=_intent(new_event=True, new_event_activity="поездка на море")))
    loop = AsyncMock(return_value="ок")
    monkeypatch.setattr(router, "run_tool_loop", loop)
    telegram_bot = AsyncMock()

    await router.handle_active_message(
        db_pool, telegram_bot, active, _message("@orgbot а поехали на море"), BOT_ID, BOT_USERNAME
    )

    loop.assert_not_awaited()
    said = telegram_bot.send_message.await_args.kwargs["text"]
    assert "пикник в парке" in said and "поездка на море" in said
    pending = await core_tools.get_pending_confirmation(db_pool, active["chat_id"])
    assert pending["action_type"] == "retopic"
    # Still the old event until somebody says yes.
    row = await db_pool.fetchrow("SELECT activity_type FROM sessions WHERE id = $1", active["id"])
    assert row["activity_type"] == "пикник в парке"


async def test_saying_yes_switches_the_event_and_keeps_the_list(db_pool, monkeypatch):
    """Retopic rather than close-and-reopen: the list and the participants are
    what the group built by hand, and a change of plan does not undo them."""
    active = await _new_active_session(db_pool, activity_type="пикник в парке")
    await core_tools.list_add(db_pool, active["id"], "мангал")
    monkeypatch.setattr(router, "_addressed_intent", AsyncMock(
        return_value=_intent(new_event=True, new_event_activity="поездка на море",
                             new_event_date="2026-09-05")))
    telegram_bot = AsyncMock()
    await router.handle_active_message(
        db_pool, telegram_bot, active, _message("@orgbot а поехали на море 5/9"), BOT_ID, BOT_USERNAME
    )

    monkeypatch.setattr(router, "extract", AsyncMock(return_value={"reply": "yes"}))
    await router.handle_active_message(
        db_pool, telegram_bot, active, _message("@orgbot да"), BOT_ID, BOT_USERNAME
    )

    row = await db_pool.fetchrow(
        "SELECT activity_type, event_date, status FROM sessions WHERE id = $1", active["id"]
    )
    assert row["activity_type"] == "поездка на море"
    assert row["event_date"] == dt.date(2026, 9, 5)
    assert row["status"] == "active"
    items = (await core_tools.list_show(db_pool, active["id"]))["items"]
    assert [i["name"] for i in items] == ["мангал"]
    assert "поездка на море" in telegram_bot.send_message.await_args.kwargs["text"]


async def test_saying_no_leaves_the_event_alone(db_pool, monkeypatch):
    active = await _new_active_session(db_pool, activity_type="пикник в парке")
    monkeypatch.setattr(router, "_addressed_intent", AsyncMock(
        return_value=_intent(new_event=True, new_event_activity="поездка на море")))
    telegram_bot = AsyncMock()
    await router.handle_active_message(
        db_pool, telegram_bot, active, _message("@orgbot а поехали на море"), BOT_ID, BOT_USERNAME
    )

    monkeypatch.setattr(router, "extract", AsyncMock(return_value={"reply": "no"}))
    await router.handle_active_message(
        db_pool, telegram_bot, active, _message("@orgbot нет"), BOT_ID, BOT_USERNAME
    )

    row = await db_pool.fetchrow("SELECT activity_type FROM sessions WHERE id = $1", active["id"])
    assert row["activity_type"] == "пикник в парке"


async def test_a_switch_with_no_new_date_keeps_the_old_one(db_pool, monkeypatch):
    """Changing what the event is says nothing about when it is, and an
    emptied date would read as "nobody has said" — which is not true."""
    active = await _new_active_session(db_pool, activity_type="пикник")
    await db_pool.execute(
        "UPDATE sessions SET event_date = $2 WHERE id = $1", active["id"], dt.date(2026, 9, 20)
    )

    updated = await session.retopic(db_pool, active["id"], "поход")

    assert updated["activity_type"] == "поход"
    assert updated["event_date"] == dt.date(2026, 9, 20)


async def test_a_new_event_is_never_treated_as_off_topic(db_pool, monkeypatch):
    """Proposing a different event is organizing, not chatter. Judged
    off-topic first, the group could not change their minds at all."""
    active = await _new_active_session(db_pool, activity_type="пикник")
    monkeypatch.setattr(router, "_addressed_intent", AsyncMock(
        return_value=_intent(new_event=True, off_topic=True, new_event_activity="поход")))
    telegram_bot = AsyncMock()

    await router.handle_active_message(
        db_pool, telegram_bot, active, _message("@orgbot давайте в поход"), BOT_ID, BOT_USERNAME
    )

    assert telegram_bot.send_message.await_args.kwargs["text"] != router._OFF_TOPIC_REPLY
    assert await core_tools.get_pending_confirmation(db_pool, active["chat_id"]) is not None


async def test_the_classifier_is_told_what_is_being_tracked(db_pool):
    """Without it, "поехали на море в субботу" said *about* the trip already
    under way is indistinguishable from proposing a different one."""
    prompt = router._intent_input("а поехали на море", [], "пикник в парке")

    assert prompt.startswith("Currently tracking: пикник в парке")


async def test_a_turn_that_never_answers_still_says_something(db_pool, monkeypatch):
    """A provider accepted a request and never answered it. There was no
    exception to catch, so no branch fired and nothing was posted — the person
    watched an empty chat while the bot waited out a 600-second timeout."""
    from bot.ai.tool_loop import TurnTooSlow

    active = await _new_active_session(db_pool)
    monkeypatch.setattr(router, "_addressed_intent", AsyncMock(
        return_value={"stop": False, "off_topic": False, "new_event": False}))
    monkeypatch.setattr(router, "run_tool_loop", AsyncMock(side_effect=TurnTooSlow("30s")))
    telegram_bot = AsyncMock()

    await router.handle_active_message(
        db_pool, telegram_bot, active, _message("@orgbot что по списку?"), BOT_ID, BOT_USERNAME
    )

    telegram_bot.send_message.assert_awaited_once_with(
        chat_id=active["chat_id"], text=router._TOOK_TOO_LONG
    )


async def test_the_deadline_message_is_not_the_refusal_one():
    """Nothing refused a turn that outran the clock, and telling somebody to
    wait for a model that is answering everyone else would be wrong."""
    assert router._TOOK_TOO_LONG not in router._AI_UNAVAILABLE_MESSAGES
    assert router._TOOK_TOO_LONG != router._FALLBACK_MESSAGE
