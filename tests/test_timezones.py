import datetime as dt
from unittest.mock import AsyncMock, patch
from zoneinfo import ZoneInfo, available_timezones

import pytest

import bot.timezones as timezones
import bot.tools.core as core


async def _chat(db_pool, chat_id=1, tz=None):
    await db_pool.execute(
        "INSERT INTO chats (chat_id, title, timezone) VALUES ($1, 'Chat', $2) ON CONFLICT DO NOTHING",
        chat_id, tz,
    )
    return await db_pool.fetchval(
        "INSERT INTO sessions (chat_id, activity_type) VALUES ($1, 'picnic') RETURNING id", chat_id
    )


def test_only_real_iana_names_are_accepted():
    """The model will happily offer "MSK" or "UTC+3"; neither is a zone."""
    assert timezones.is_valid_timezone("Europe/Moscow") is True
    assert timezones.is_valid_timezone("MSK") is False
    assert timezones.is_valid_timezone("UTC+3") is False
    assert timezones.is_valid_timezone("") is False
    assert timezones.is_valid_timezone(None) is False


def test_a_naive_wall_clock_time_is_read_in_the_chats_zone():
    """"напомни в 9 утра" means 9am where the group is. Reading it as UTC is
    what made a UTC+3 group's reminders fire three hours late."""
    nine_am = dt.datetime(2026, 8, 26, 9, 0)

    as_utc = timezones.to_utc(nine_am, ZoneInfo("Europe/Moscow"))

    assert as_utc == dt.datetime(2026, 8, 26, 6, 0, tzinfo=dt.timezone.utc)


def test_an_explicit_offset_is_respected_not_overridden():
    aware = dt.datetime(2026, 8, 26, 9, 0, tzinfo=dt.timezone.utc)

    assert timezones.to_utc(aware, ZoneInfo("Europe/Moscow")) == aware


async def test_unknown_chat_falls_back_to_the_default(db_pool):
    await _chat(db_pool, chat_id=1, tz=None)

    assert await timezones.stored_timezone(db_pool, 1) is None
    assert str(await timezones.chat_timezone(db_pool, 1)) == timezones.DEFAULT_TIMEZONE


async def test_a_corrupt_stored_zone_falls_back_rather_than_raising(db_pool):
    """Being an hour off beats the whole reminder path breaking."""
    await _chat(db_pool, chat_id=1, tz=None)
    await db_pool.execute("UPDATE chats SET timezone = 'Mars/Olympus' WHERE chat_id = 1")

    assert str(await timezones.chat_timezone(db_pool, 1)) == timezones.DEFAULT_TIMEZONE


async def test_set_chat_timezone_refuses_a_made_up_zone(db_pool):
    await _chat(db_pool, chat_id=1)

    assert await timezones.set_chat_timezone(db_pool, 1, "MSK") is False
    assert await db_pool.fetchval("SELECT timezone FROM chats WHERE chat_id = 1") is None


async def test_timezone_is_learned_from_a_resolved_place(db_pool):
    """The one moment the group's zone is available for free."""
    await _chat(db_pool, chat_id=1)
    payload = AsyncMock()
    payload.json = lambda: {"timezone": "Europe/Moscow"}
    payload.raise_for_status = lambda: None
    client = AsyncMock()
    client.__aenter__.return_value.get = AsyncMock(return_value=payload)

    with patch("bot.timezones.httpx.AsyncClient", return_value=client):
        learned = await timezones.learn_timezone_from_coordinates(db_pool, 1, 55.75, 37.62)

    assert learned == "Europe/Moscow"
    assert await db_pool.fetchval("SELECT timezone FROM chats WHERE chat_id = 1") == "Europe/Moscow"


async def test_learning_never_overwrites_what_a_human_said(db_pool):
    """Someone stating where they live outranks the coordinates of a
    restaurant they happened to look up.

    Set through set_chat_timezone rather than written into the row, because
    that is what marks it as stated. A zone with no recorded source is a
    guess, and a guess is replaceable — see
    test_a_later_lookup_replaces_an_earlier_one.
    """
    await _chat(db_pool, chat_id=1)
    await timezones.set_chat_timezone(db_pool, 1, "Europe/Moscow")

    with patch("bot.timezones.httpx.AsyncClient") as client:
        result = await timezones.learn_timezone_from_coordinates(db_pool, 1, 32.07, 34.79)

    client.assert_not_called()
    assert result == "Europe/Moscow"


async def test_a_failed_lookup_does_not_break_the_caller(db_pool):
    await _chat(db_pool, chat_id=1)

    with patch("bot.timezones.httpx.AsyncClient", side_effect=RuntimeError("network down")):
        assert await timezones.learn_timezone_from_coordinates(db_pool, 1, 55.75, 37.62) is None

    assert await db_pool.fetchval("SELECT timezone FROM chats WHERE chat_id = 1") is None


async def test_reminder_set_says_when_it_had_to_assume_a_zone(db_pool):
    session_id = await _chat(db_pool, chat_id=1)

    result = await core.reminder_set(db_pool, session_id, "выезжаем", "2026-08-26T09:00:00")

    assert result["status"] == "ok"
    assert result["timezone_assumed"] == timezones.DEFAULT_TIMEZONE
    assert "ask_user" in result


async def test_reminder_set_does_not_ask_when_the_zone_is_known(db_pool):
    session_id = await _chat(db_pool, chat_id=1, tz="Europe/Moscow")

    result = await core.reminder_set(db_pool, session_id, "выезжаем", "2026-08-26T09:00:00")

    assert "timezone_assumed" not in result
    stored = await db_pool.fetchval("SELECT remind_at FROM reminders WHERE id = $1", result["reminder_id"])
    assert stored == dt.datetime(2026, 8, 26, 6, 0, tzinfo=dt.timezone.utc)


async def test_naming_the_timezone_corrects_reminders_already_scheduled(db_pool):
    """The whole point of asking: a reminder set before anyone said where they
    are must move to the right absolute moment once they do."""
    session_id = await _chat(db_pool, chat_id=1)
    created = await core.reminder_set(db_pool, session_id, "выезжаем", "2026-08-26T09:00:00")

    result = await core.set_timezone(db_pool, session_id, "Europe/Moscow")

    assert result["status"] == "ok"
    assert result["reminders_corrected"] == 1
    stored = await db_pool.fetchval("SELECT remind_at FROM reminders WHERE id = $1", created["reminder_id"])
    assert stored == dt.datetime(2026, 8, 26, 6, 0, tzinfo=dt.timezone.utc)


async def test_correction_is_exact_across_a_dst_boundary(db_pool):
    """Re-anchoring recomputes from the stored wall-clock time rather than
    shifting by an offset delta, so a reminder on the far side of a DST change
    still lands at the hour it was asked for."""
    session_id = await _chat(db_pool, chat_id=1)
    # 1 Feb: Moscow has no DST, New York is on standard time (UTC-5).
    created = await core.reminder_set(db_pool, session_id, "звонок", "2027-02-01T09:00:00")

    await core.set_timezone(db_pool, session_id, "America/New_York")

    stored = await db_pool.fetchval("SELECT remind_at FROM reminders WHERE id = $1", created["reminder_id"])
    assert stored == dt.datetime(2027, 2, 1, 14, 0, tzinfo=dt.timezone.utc)
    local = stored.astimezone(ZoneInfo("America/New_York"))
    assert (local.hour, local.minute) == (9, 0)


async def test_an_already_sent_reminder_is_not_moved(db_pool):
    session_id = await _chat(db_pool, chat_id=1)
    created = await core.reminder_set(db_pool, session_id, "выезжаем", "2026-08-26T09:00:00")
    await db_pool.execute("UPDATE reminders SET status = 'sent' WHERE id = $1", created["reminder_id"])
    before = await db_pool.fetchval("SELECT remind_at FROM reminders WHERE id = $1", created["reminder_id"])

    result = await core.set_timezone(db_pool, session_id, "Europe/Moscow")

    assert result["reminders_corrected"] == 0
    assert await db_pool.fetchval("SELECT remind_at FROM reminders WHERE id = $1", created["reminder_id"]) == before


async def test_set_timezone_tool_refuses_a_made_up_zone(db_pool):
    session_id = await _chat(db_pool, chat_id=1)

    result = await core.set_timezone(db_pool, session_id, "MSK")

    assert result["status"] == "bad_timezone"


async def _dated_session(db_pool, chat_id, tz, event_date):
    await db_pool.execute(
        "INSERT INTO chats (chat_id, title, timezone) VALUES ($1, 'Chat', $2)", chat_id, tz
    )
    return await db_pool.fetchval(
        """
        INSERT INTO sessions (chat_id, activity_type, event_date)
        VALUES ($1, 'picnic', $2) RETURNING id
        """,
        chat_id, event_date,
    )


async def test_the_closing_question_follows_the_chats_local_day(db_pool):
    """"The day after the event" has to mean the day after *where the group
    is*. Anchoring each chat to its own local date makes this deterministic
    whatever the server clock says: an event dated the chat's local yesterday
    is over, one dated its local today is not.

    Kiritimati (UTC+14) and Midway (UTC-11) are 25 hours apart, so on any given
    run at least one of them is on a different calendar day from the server —
    which is exactly the situation the old `current_date` logic got wrong.
    """
    import bot.session as session

    over, not_over = [], []
    for chat_id, zone in ((1, "Pacific/Kiritimati"), (2, "Pacific/Midway"),
                          (3, "Europe/Moscow"), (4, "America/New_York")):
        local_today = dt.datetime.now(ZoneInfo(zone)).date()
        over.append(await _dated_session(db_pool, chat_id, zone, local_today - dt.timedelta(days=1)))
        not_over.append(await _dated_session(db_pool, chat_id + 100, zone, local_today))

    due = {r["id"] for r in await session.sessions_needing_closing_question(db_pool)}

    assert set(over) <= due, "an event that ended yesterday locally must be asked about"
    assert not (set(not_over) & due), "an event still happening today locally must not be"


async def _due_at_local_hour(db_pool, hour, monkeypatch):
    """Put a chat in whichever zone makes it `hour` o'clock there right now,
    give it an event that ended yesterday locally, and ask whether the bot
    would speak. Shifting the chat rather than the clock keeps this a real
    query against a real database."""
    import bot.session as session

    monkeypatch.setattr(session, "QUIET_UNTIL_HOUR", 9)
    monkeypatch.setattr(session, "QUIET_FROM_HOUR", 21)

    # Only zones Postgres also knows: zoneinfo carries 113 legacy aliases that
    # `AT TIME ZONE` rejects, and picking one made this test fail at random.
    pg_zones = {r["name"] for r in await db_pool.fetch("SELECT name FROM pg_timezone_names")}
    for zone in sorted(pg_zones):
        if zone in available_timezones() and dt.datetime.now(ZoneInfo(zone)).hour == hour:
            break
    else:
        pytest.skip(f"no usable zone is currently at {hour}:00")

    local_today = dt.datetime.now(ZoneInfo(zone)).date()
    session_id = await _dated_session(db_pool, 1, zone, local_today - dt.timedelta(days=1))
    due = {r["id"] for r in await session.sessions_needing_closing_question(db_pool)}
    return session_id in due, zone


async def test_the_bot_does_not_start_a_conversation_at_local_midnight(db_pool, monkeypatch):
    """Getting the local day right made the question come due the moment the
    date rolls over — around local midnight, which is a rude time to message a
    group."""
    would_speak, zone = await _due_at_local_hour(db_pool, 0, monkeypatch)

    assert would_speak is False, f"bot would have posted at 00:00 in {zone}"


async def test_the_bot_does_not_start_a_conversation_late_at_night(db_pool, monkeypatch):
    would_speak, zone = await _due_at_local_hour(db_pool, 22, monkeypatch)

    assert would_speak is False, f"bot would have posted at 22:00 in {zone}"


async def test_the_question_goes_out_once_the_group_is_awake(db_pool, monkeypatch):
    """Delayed, not skipped: the worker polls every minute, so a question that
    came due overnight goes out at the start of the window."""
    would_speak, zone = await _due_at_local_hour(db_pool, 10, monkeypatch)

    assert would_speak is True, f"bot stayed silent at 10:00 in {zone}"


async def test_a_zone_postgres_cannot_use_is_refused(db_pool):
    """zoneinfo knows 599 zones, Postgres 487. Storing one of the 113 legacy
    aliases would make the closing-question query raise for the whole batch —
    one chat's bad zone silencing the bot in every chat."""
    await _chat(db_pool, chat_id=1)

    assert timezones.is_valid_timezone("US/Hawaii") is True, "Python accepts it"
    assert await timezones.known_to_postgres(db_pool, "US/Hawaii") is False
    assert await timezones.set_chat_timezone(db_pool, 1, "US/Hawaii") is False
    assert await db_pool.fetchval("SELECT timezone FROM chats WHERE chat_id = 1") is None


async def test_a_zone_both_agree_on_is_stored(db_pool):
    await _chat(db_pool, chat_id=1)

    assert await timezones.set_chat_timezone(db_pool, 1, "Pacific/Honolulu") is True
    assert await db_pool.fetchval("SELECT timezone FROM chats WHERE chat_id = 1") == "Pacific/Honolulu"


# --- a guess can be corrected, a statement cannot -------------------------

async def _resolve(db_pool, chat_id, name, lat=0.0, lon=0.0, monkeypatch=None):
    """learn_timezone_from_coordinates with the HTTP lookup answering `name`."""
    class _Resp:
        @staticmethod
        def raise_for_status():
            pass

        @staticmethod
        def json():
            return {"timezone": name}

    class _Client:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

        async def get(self, url, params=None):
            return _Resp()

    monkeypatch.setattr(timezones.httpx, "AsyncClient", lambda **kw: _Client())
    return await timezones.learn_timezone_from_coordinates(db_pool, chat_id, lat, lon)


async def test_a_later_lookup_replaces_an_earlier_one(db_pool, monkeypatch):
    """Found by running the manual plan. A place typed as "Бен & Co" resolved
    to a jeweller in Pretoria, the chat became Africa/Johannesburg, and the
    group saying "мы в Тель-Авиве" afterwards moved the place but not the
    zone — leaving every reminder an hour out, with nothing on screen to
    explain it.

    The rule was "never overwrite", written to protect a human's word. It
    protected one bad lookup just as firmly.
    """
    await _chat(db_pool, chat_id=1)
    await _resolve(db_pool, 1, "Africa/Johannesburg", monkeypatch=monkeypatch)

    learned = await _resolve(db_pool, 1, "Asia/Jerusalem", monkeypatch=monkeypatch)

    assert learned == "Asia/Jerusalem"
    assert await timezones.stored_timezone(db_pool, 1) == "Asia/Jerusalem"


async def test_a_row_from_before_the_column_counts_as_a_guess(db_pool, monkeypatch):
    """The safe reading: that is what most of them were, and the alternative
    is a chat stuck on a guess forever."""
    await _chat(db_pool, chat_id=1)
    await db_pool.execute(
        "UPDATE chats SET timezone = 'Africa/Johannesburg', timezone_source = NULL "
        "WHERE chat_id = 1"
    )

    learned = await _resolve(db_pool, 1, "Asia/Jerusalem", monkeypatch=monkeypatch)

    assert learned == "Asia/Jerusalem"


async def test_a_human_statement_is_recorded_as_such(db_pool):
    await _chat(db_pool, chat_id=1)

    await timezones.set_chat_timezone(db_pool, 1, "Asia/Jerusalem")

    assert await db_pool.fetchval(
        "SELECT timezone_source FROM chats WHERE chat_id = 1"
    ) == "stated"


async def test_a_refused_zone_records_no_source(db_pool):
    """A hallucinated "MSK" must not leave the chat marked as having been
    told anything."""
    await _chat(db_pool, chat_id=1)

    assert await timezones.set_chat_timezone(db_pool, 1, "MSK") is False
    assert await db_pool.fetchval(
        "SELECT timezone_source FROM chats WHERE chat_id = 1"
    ) is None
