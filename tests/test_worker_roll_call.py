"""The roll call as the worker runs it: three rounds, then the summary."""

from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from unittest.mock import AsyncMock

import pytest

import bot.session as session
import bot.tools.core as core
import worker.roll_call as worker_roll_call


@pytest.fixture
def real_quiet_hours(monkeypatch):
    """conftest turns quiet hours off for the whole suite, so the tests that
    are actually about them have to put the real window back."""
    monkeypatch.setattr(session, "QUIET_UNTIL_HOUR", 9)
    monkeypatch.setattr(session, "QUIET_FROM_HOUR", 21)


def _zone_where_the_hour_is(wanted) -> str:
    """A timezone in which it is right now one of `wanted` hours.

    Computed rather than written down: a fixed zone would put the test inside
    quiet hours only for part of the day, and the failure would look like a
    bug in the code rather than in the clock.
    """
    now = datetime.now(timezone.utc)
    for offset in range(-12, 15):
        # Etc/GMT+N is UTC-N — the sign is inverted, which is a POSIX quirk,
        # not a mistake here.
        name = f"Etc/GMT{'-' if offset > 0 else '+'}{abs(offset)}"
        if now.astimezone(ZoneInfo(name)).hour in wanted:
            return name
    raise AssertionError("no fixed-offset zone matched; the clock is impossible")


async def _session_with(db_pool, chat_id, *, tz=None, members=None):
    await db_pool.execute(
        "INSERT INTO chats (chat_id, title, timezone, member_count) VALUES ($1, 'Chat', $2, $3)",
        chat_id, tz, members,
    )
    row = await session.start_session(db_pool, chat_id=chat_id, activity_type="picnic")
    return row["id"]


async def _start_now(db_pool, session_id, chat_id):
    """A roll call due immediately, with no round spent yet — the state the
    worker sees when it picks up a run the tool started."""
    row = await session.start_roll_call(db_pool, session_id, chat_id)
    return row["id"]


def _texts(telegram_bot):
    return [c.kwargs["text"] for c in telegram_bot.send_message.await_args_list]


async def _make_due(db_pool, roll_call_id):
    await db_pool.execute(
        "UPDATE roll_calls SET next_at = now() - interval '1 minute' WHERE id = $1",
        roll_call_id,
    )


# --- the run --------------------------------------------------------------

async def test_three_rounds_then_the_summary(db_pool):
    session_id = await _session_with(db_pool, -900)
    await core.set_participant(db_pool, session_id, "Игорёк", "unknown")
    roll_call_id = await _start_now(db_pool, session_id, -900)
    telegram_bot = AsyncMock()

    for _ in range(4):
        await _make_due(db_pool, roll_call_id)
        await worker_roll_call.run_roll_calls(db_pool, telegram_bot)

    texts = _texts(telegram_bot)
    assert len(texts) == 4
    assert all("Ещё не сказали" in t for t in texts[:3])
    assert "Не ответили:" in texts[3]

    # And nothing after it: the run is done, not merely quiet.
    await _make_due(db_pool, roll_call_id)
    await worker_roll_call.run_roll_calls(db_pool, telegram_bot)
    assert len(_texts(telegram_bot)) == 4


async def test_someone_who_answered_drops_out_of_the_next_round(db_pool):
    session_id = await _session_with(db_pool, -901)
    await core.set_participant(db_pool, session_id, "Игорёк", "unknown")
    await core.set_participant(db_pool, session_id, "Бердыев", "unknown")
    roll_call_id = await _start_now(db_pool, session_id, -901)
    telegram_bot = AsyncMock()

    await worker_roll_call.run_roll_calls(db_pool, telegram_bot)
    await core.set_participant(db_pool, session_id, "Игорёк", "confirmed")
    await _make_due(db_pool, roll_call_id)
    await worker_roll_call.run_roll_calls(db_pool, telegram_bot)

    first, second = _texts(telegram_bot)
    assert "Игорёк" in first
    assert "Игорёк" not in second
    assert "Бердыев" in second


async def test_everyone_answering_ends_the_run_early(db_pool):
    """Two more questions naming nobody would read as the bot malfunctioning."""
    session_id = await _session_with(db_pool, -902)
    await core.set_participant(db_pool, session_id, "Игорёк", "unknown")
    roll_call_id = await _start_now(db_pool, session_id, -902)
    telegram_bot = AsyncMock()

    await worker_roll_call.run_roll_calls(db_pool, telegram_bot)
    await core.set_participant(db_pool, session_id, "Игорёк", "confirmed")
    await _make_due(db_pool, roll_call_id)
    result = await worker_roll_call.run_roll_calls(db_pool, telegram_bot)

    assert result["summarised"] == [roll_call_id]
    assert "Ответили все." in _texts(telegram_bot)[1]
    assert await db_pool.fetchval(
        "SELECT status FROM roll_calls WHERE id = $1", roll_call_id
    ) == "done"


async def test_the_summary_reports_the_people_the_bot_cannot_name(db_pool):
    session_id = await _session_with(db_pool, -903, members=10)
    await core.set_participant(db_pool, session_id, "Игорёк", "confirmed")
    roll_call_id = await _start_now(db_pool, session_id, -903)
    telegram_bot = AsyncMock()

    await worker_roll_call.run_roll_calls(db_pool, telegram_bot)

    assert await db_pool.fetchval(
        "SELECT status FROM roll_calls WHERE id = $1", roll_call_id
    ) == "done"
    # 10 in the chat, 1 recorded, minus the bot itself.
    assert "ещё 8 человек" in _texts(telegram_bot)[0]


# --- failure and quiet hours ----------------------------------------------

async def test_a_failed_send_does_not_spend_a_round(db_pool):
    """A round marked delivered when nothing was posted silently costs the
    group one of the three it was promised."""
    session_id = await _session_with(db_pool, -904)
    await core.set_participant(db_pool, session_id, "Игорёк", "unknown")
    roll_call_id = await _start_now(db_pool, session_id, -904)
    before = await db_pool.fetchrow(
        "SELECT rounds_done, next_at FROM roll_calls WHERE id = $1", roll_call_id
    )

    telegram_bot = AsyncMock()
    telegram_bot.send_message = AsyncMock(side_effect=Exception("chat not found"))
    await worker_roll_call.run_roll_calls(db_pool, telegram_bot)

    after = await db_pool.fetchrow(
        "SELECT rounds_done, next_at FROM roll_calls WHERE id = $1", roll_call_id
    )
    assert after["rounds_done"] == before["rounds_done"]
    assert after["next_at"] == before["next_at"]


async def test_nothing_goes_out_during_quiet_hours(db_pool, real_quiet_hours):
    session_id = await _session_with(
        db_pool, -905, tz=_zone_where_the_hour_is(range(21, 24))
    )
    await core.set_participant(db_pool, session_id, "Игорёк", "unknown")
    roll_call_id = await _start_now(db_pool, session_id, -905)
    telegram_bot = AsyncMock()

    await worker_roll_call.run_roll_calls(db_pool, telegram_bot)

    telegram_bot.send_message.assert_not_awaited()
    # And the round is deferred, not spent — this is the whole reason the
    # waking-hours check sits in the claim rather than around the send.
    assert await db_pool.fetchval(
        "SELECT rounds_done FROM roll_calls WHERE id = $1", roll_call_id
    ) == 0


async def test_the_deferred_round_goes_out_once_the_group_is_awake(db_pool, real_quiet_hours):
    session_id = await _session_with(
        db_pool, -906, tz=_zone_where_the_hour_is(range(9, 21))
    )
    await core.set_participant(db_pool, session_id, "Игорёк", "unknown")
    await _start_now(db_pool, session_id, -906)
    telegram_bot = AsyncMock()

    await worker_roll_call.run_roll_calls(db_pool, telegram_bot)

    telegram_bot.send_message.assert_awaited_once()


# --- the lifecycle --------------------------------------------------------

async def test_closing_the_session_stops_the_roll_call(db_pool):
    """A run that outlived its event would keep asking about a picnic the
    group already went to."""
    session_id = await _session_with(db_pool, -907)
    await core.set_participant(db_pool, session_id, "Игорёк", "unknown")
    roll_call_id = await _start_now(db_pool, session_id, -907)

    await session.close_session(db_pool, session_id, reason="explicit_stop")

    assert await db_pool.fetchval(
        "SELECT status FROM roll_calls WHERE id = $1", roll_call_id
    ) == "cancelled"
    assert await worker_roll_call.run_roll_calls(db_pool, AsyncMock()) == {
        "asked": [], "summarised": []
    }


async def test_switching_event_stops_the_roll_call(db_pool):
    session_id = await _session_with(db_pool, -908)
    roll_call_id = await _start_now(db_pool, session_id, -908)

    await session.retopic(db_pool, session_id, "поездка на море")

    assert await db_pool.fetchval(
        "SELECT status FROM roll_calls WHERE id = $1", roll_call_id
    ) == "cancelled"


async def test_only_one_run_per_session(db_pool):
    session_id = await _session_with(db_pool, -909)
    await _start_now(db_pool, session_id, -909)

    assert await session.start_roll_call(db_pool, session_id, -909) is None


async def test_a_finished_run_lets_a_new_one_start(db_pool):
    session_id = await _session_with(db_pool, -910)
    roll_call_id = await _start_now(db_pool, session_id, -910)

    await session.finish_roll_call(db_pool, roll_call_id)

    assert await session.start_roll_call(db_pool, session_id, -910) is not None
