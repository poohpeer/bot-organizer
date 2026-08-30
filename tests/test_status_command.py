"""/status — the organizing report without spending a model turn.

Asking "как дела с организацией" costs a turn of the primary chain and
depends on the model relaying the report verbatim. bot/turn_outcome.py exists
to force that, which is itself evidence it does not always happen. A command
has neither the cost nor the failure mode.
"""

from types import SimpleNamespace
from unittest.mock import AsyncMock

import bot.session as session
import bot.status_command as status_command
import bot.tools.core as core_tools


def _message(chat_id=-100, user_id=7):
    return SimpleNamespace(
        chat=SimpleNamespace(id=chat_id),
        from_user=SimpleNamespace(id=user_id),
    )


async def _active(db_pool, chat_id=-100):
    await db_pool.execute(
        "INSERT INTO chats (chat_id, title) VALUES ($1, 'Chat') ON CONFLICT DO NOTHING", chat_id
    )
    return await session.start_session(db_pool, chat_id=chat_id, activity_type="picnic")


async def test_it_posts_the_report_into_the_chat(db_pool):
    active = await _active(db_pool)
    await core_tools.remember_fact(db_pool, active["id"], "place", "Маленькая прага")
    await core_tools.list_add(db_pool, active["id"], "пиво", amount=2, unit="бутылка")
    telegram_bot = AsyncMock()

    await status_command.handle_command(db_pool, telegram_bot, _message())

    sent = telegram_bot.send_message.await_args.kwargs
    assert sent["chat_id"] == -100
    assert "Маленькая прага" in sent["text"]
    assert "пиво" in sent["text"]
    assert "🛒" in sent["text"], "the whole report, not just the parts one caller wanted"


async def test_it_is_the_same_block_the_model_would_have_relayed(db_pool):
    """Two renderings of the same state that could drift apart is exactly the
    problem status_render was written to end."""
    import bot.tools.composed as composed

    active = await _active(db_pool)
    await core_tools.remember_fact(db_pool, active["id"], "place", "Море")
    telegram_bot = AsyncMock()

    await status_command.handle_command(db_pool, telegram_bot, _message())

    assert telegram_bot.send_message.await_args.kwargs["text"] == (
        await composed.event_status(db_pool, active["id"])
    )["report"]


async def test_it_never_calls_a_model(db_pool, monkeypatch):
    """The point of the command. A boom here means the report went through
    the chain after all."""
    import bot.ai.classify as classify

    def boom(*args, **kwargs):
        raise AssertionError("/status must not spend a model call")

    monkeypatch.setattr(classify, "extract", boom)
    monkeypatch.setattr(classify, "classify", boom)
    await _active(db_pool)

    await status_command.handle_command(db_pool, AsyncMock(), _message())


async def test_a_chat_with_nothing_tracked_is_told_so(db_pool):
    await db_pool.execute(
        "INSERT INTO chats (chat_id, title) VALUES (-100, 'Chat') ON CONFLICT DO NOTHING"
    )
    telegram_bot = AsyncMock()

    await status_command.handle_command(db_pool, telegram_bot, _message())

    assert telegram_bot.send_message.await_args.kwargs["text"] == status_command.NO_SESSION


async def test_one_chats_status_is_not_anothers(db_pool):
    """chat_id comes from the message, never from anything a caller supplies
    — the same rule every tool follows."""
    theirs = await _active(db_pool, chat_id=-100)
    await _active(db_pool, chat_id=-200)
    await core_tools.remember_fact(db_pool, theirs["id"], "place", "Маленькая прага")
    telegram_bot = AsyncMock()

    await status_command.handle_command(db_pool, telegram_bot, _message(chat_id=-200))

    assert "Маленькая прага" not in telegram_bot.send_message.await_args.kwargs["text"]


async def test_reminders_answers_with_the_schedule_alone(db_pool):
    """The same block /status carries under ⏰, without the rest of it."""
    import datetime as dt

    active = await _active(db_pool)
    await core_tools.remember_fact(db_pool, active["id"], "place", "Маленькая прага")
    await core_tools.reminder_set(
        db_pool, active["id"],
        message="взять мангал",
        remind_at=(dt.datetime.now(dt.timezone.utc) + dt.timedelta(hours=2)).isoformat(),
    )
    telegram_bot = AsyncMock()

    await status_command.handle_command(db_pool, telegram_bot, _message(), "reminders")

    text = telegram_bot.send_message.await_args.kwargs["text"]
    assert "взять мангал" in text
    assert "Маленькая прага" not in text, "the schedule alone, not the whole report"
    assert "🛒" not in text


async def test_reminders_says_plainly_when_there_are_none(db_pool):
    """"Nothing scheduled" is an answer. Saying nothing, or claiming it cannot
    check, is what this whole area exists to stop."""
    await _active(db_pool)
    telegram_bot = AsyncMock()

    await status_command.handle_command(db_pool, telegram_bot, _message(), "reminders")

    assert telegram_bot.send_message.await_args.kwargs["text"] == "Нет запланированных напоминаний"


async def test_the_schedule_reads_the_same_here_as_in_the_status(db_pool):
    """One renderer, so the command and the report cannot drift apart."""
    import datetime as dt
    import bot.tools.composed as composed

    active = await _active(db_pool)
    await core_tools.reminder_set(
        db_pool, active["id"],
        message="выехать",
        remind_at=(dt.datetime.now(dt.timezone.utc) + dt.timedelta(hours=3)).isoformat(),
    )
    telegram_bot = AsyncMock()

    await status_command.handle_command(db_pool, telegram_bot, _message(), "reminders")

    schedule = telegram_bot.send_message.await_args.kwargs["text"]
    assert schedule in (await composed.event_status(db_pool, active["id"]))["report"]
