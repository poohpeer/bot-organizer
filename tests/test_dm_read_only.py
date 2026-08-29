"""Reading the organizing state privately, without disturbing the group.

Read-only on purpose: every change made in the group is visible to everyone
there, and that visibility is most of what makes a shared list trustworthy.
Looking is different — it disturbs nobody, which is the whole reason for
asking privately.

The gate is membership, asked of Telegram every time. A private chat has no
session of its own, so without it the only thing standing between a stranger
and a group's plans would be a row in decision_log.
"""

from types import SimpleNamespace
from unittest.mock import AsyncMock

import bot.dm as dm
import bot.session as session
import bot.status_command as status_command
import bot.tools.core as core_tools

ME = 7
STRANGER = 8


def _dm(user_id=ME, chat_id=ME):
    return SimpleNamespace(
        chat=SimpleNamespace(id=chat_id, type="private"),
        from_user=SimpleNamespace(id=user_id),
    )


def _query(data, user_id=ME):
    return SimpleNamespace(
        data=data,
        from_user=SimpleNamespace(id=user_id),
        message=SimpleNamespace(chat=SimpleNamespace(id=user_id, type="private")),
        answer=AsyncMock(),
    )


def _bot(member=True):
    telegram_bot = AsyncMock()
    telegram_bot.get_chat_member.return_value = SimpleNamespace(
        status="member" if member else "left"
    )
    return telegram_bot


async def _session_with(db_pool, chat_id, *, title, user_id=ME, place=None, item=None):
    await db_pool.execute(
        "INSERT INTO chats (chat_id, title) VALUES ($1, $2) ON CONFLICT (chat_id) "
        "DO UPDATE SET title = EXCLUDED.title",
        chat_id, title,
    )
    active = await session.start_session(db_pool, chat_id=chat_id, activity_type="picnic")
    await core_tools.set_participant(
        db_pool, active["id"], "Alex", "confirmed", user_id=user_id
    )
    if place:
        await core_tools.remember_fact(db_pool, active["id"], "place", place)
    if item:
        await core_tools.list_add(db_pool, active["id"], item, amount=2, unit="бутылка")
    return active


# --- one active event -----------------------------------------------------

async def test_status_in_a_dm_answers_about_the_one_active_event(db_pool):
    await _session_with(db_pool, -100, title="Море", place="Маленькая прага")
    telegram_bot = _bot()

    await status_command.handle_command(db_pool, telegram_bot, _dm(), "status")

    sent = telegram_bot.send_message.await_args.kwargs
    assert sent["chat_id"] == ME, "the answer goes to the private chat, not the group"
    assert "Маленькая прага" in sent["text"]


async def test_list_in_a_dm_answers_with_the_list(db_pool):
    await _session_with(db_pool, -100, title="Море", item="пиво")
    telegram_bot = _bot()

    await status_command.handle_command(db_pool, telegram_bot, _dm(), "list")

    text = telegram_bot.send_message.await_args.kwargs["text"]
    assert "пиво" in text
    assert "👥" not in text, "/list is the list, not the whole report"


async def test_nothing_is_posted_into_the_group(db_pool):
    """The entire point of asking privately."""
    await _session_with(db_pool, -100, title="Море", place="Маленькая прага")
    telegram_bot = _bot()

    await status_command.handle_command(db_pool, telegram_bot, _dm(), "status")

    for call in telegram_bot.send_message.await_args_list:
        assert call.kwargs["chat_id"] != -100


# --- the membership gate --------------------------------------------------

async def test_someone_who_has_left_is_told_nothing(db_pool):
    """decision_log remembers everyone who ever spoke. Being in it is not
    permission; being in the chat now is."""
    await _session_with(db_pool, -100, title="Море", place="Маленькая прага")
    telegram_bot = _bot(member=False)

    await status_command.handle_command(db_pool, telegram_bot, _dm(), "status")

    text = telegram_bot.send_message.await_args.kwargs["text"]
    assert text == dm.NOTHING_TRACKED
    assert "Маленькая прага" not in text


async def test_a_failed_membership_check_answers_no(db_pool):
    """A private read is the wrong place to fail open."""
    await _session_with(db_pool, -100, title="Море", place="Маленькая прага")
    telegram_bot = AsyncMock()
    telegram_bot.get_chat_member.side_effect = RuntimeError("telegram is unwell")

    await status_command.handle_command(db_pool, telegram_bot, _dm(), "status")

    assert telegram_bot.send_message.await_args.kwargs["text"] == dm.NOTHING_TRACKED


async def test_a_stranger_sees_nothing(db_pool):
    await _session_with(db_pool, -100, title="Море", place="Маленькая прага")
    telegram_bot = _bot(member=False)

    await status_command.handle_command(
        db_pool, telegram_bot, _dm(user_id=STRANGER), "status"
    )

    assert telegram_bot.send_message.await_args.kwargs["text"] == dm.NOTHING_TRACKED


async def test_membership_is_asked_every_time_not_cached(db_pool):
    """Someone who leaves must stop seeing the list immediately."""
    await _session_with(db_pool, -100, title="Море")
    telegram_bot = _bot()

    await status_command.handle_command(db_pool, telegram_bot, _dm(), "status")
    await status_command.handle_command(db_pool, telegram_bot, _dm(), "status")

    assert telegram_bot.get_chat_member.await_count == 2


# --- more than one --------------------------------------------------------

async def test_several_events_are_offered_as_buttons(db_pool):
    await _session_with(db_pool, -100, title="Море")
    await _session_with(db_pool, -200, title="Дача")
    telegram_bot = _bot()

    await status_command.handle_command(db_pool, telegram_bot, _dm(), "status")

    sent = telegram_bot.send_message.await_args.kwargs
    assert sent["text"] == dm.PICK_A_CHAT
    labels = [row[0].text for row in sent["reply_markup"].inline_keyboard]
    assert set(labels) == {"Море", "Дача"}


async def test_the_button_remembers_which_command_was_asked(db_pool):
    """A person who typed /list must not get a status report because the
    keyboard forgot what they wanted."""
    await _session_with(db_pool, -100, title="Море", item="пиво")
    await _session_with(db_pool, -200, title="Дача")
    telegram_bot = _bot()

    await status_command.handle_command(db_pool, telegram_bot, _dm(), "list")
    keyboard = telegram_bot.send_message.await_args.kwargs["reply_markup"]
    button = next(
        row[0] for row in keyboard.inline_keyboard if row[0].text == "Море"
    )

    telegram_bot.send_message.reset_mock()
    await status_command.handle_pick(db_pool, telegram_bot, _query(button.callback_data))

    text = telegram_bot.send_message.await_args.kwargs["text"]
    assert "пиво" in text
    assert "👥" not in text


async def test_a_pressed_button_is_re_checked_against_membership(db_pool):
    """The keyboard stays live in the private chat, and membership can end
    after it was drawn — callback data is not a permission."""
    active = await _session_with(db_pool, -100, title="Море", place="Маленькая прага")
    telegram_bot = _bot(member=False)

    query = _query(f"{dm.PICK}status:{active['id']}")
    await status_command.handle_pick(db_pool, telegram_bot, query)

    telegram_bot.send_message.assert_not_awaited()
    assert query.answer.await_args.kwargs.get("show_alert") is True


async def test_a_button_naming_someone_elses_session_gives_nothing(db_pool):
    """Callback data is user-supplied. A hand-crafted session id must not
    open a group this person is not in."""
    theirs = await _session_with(db_pool, -100, title="Море", place="Маленькая прага")
    telegram_bot = _bot(member=False)

    query = _query(f"{dm.PICK}status:{theirs['id']}", user_id=STRANGER)
    await status_command.handle_pick(db_pool, telegram_bot, query)

    telegram_bot.send_message.assert_not_awaited()


async def test_junk_callback_data_is_ignored(db_pool):
    telegram_bot = _bot()

    for data in (f"{dm.PICK}status:abc", f"{dm.PICK}delete:1", "dm:", ""):
        query = _query(data)
        await status_command.handle_pick(db_pool, telegram_bot, query)

    telegram_bot.send_message.assert_not_awaited()


# --- nothing to show ------------------------------------------------------

async def test_a_person_with_no_events_is_told_so(db_pool):
    telegram_bot = _bot()

    await status_command.handle_command(db_pool, telegram_bot, _dm(), "status")

    assert telegram_bot.send_message.await_args.kwargs["text"] == dm.NOTHING_TRACKED


async def test_a_closed_event_is_not_offered(db_pool):
    active = await _session_with(db_pool, -100, title="Море")
    await session.close_session(db_pool, active["id"], reason="explicit_stop")
    telegram_bot = _bot()

    await status_command.handle_command(db_pool, telegram_bot, _dm(), "status")

    assert telegram_bot.send_message.await_args.kwargs["text"] == dm.NOTHING_TRACKED
