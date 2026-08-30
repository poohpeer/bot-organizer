import inspect

import bot.tools.core as core
import bot.tools.schema as schema


async def _new_session(db_pool, chat_id=1, tz=None):
    await db_pool.execute(
        "INSERT INTO chats (chat_id, title, timezone) VALUES ($1, 'Chat', $2)", chat_id, tz
    )
    row = await db_pool.fetchrow(
        "INSERT INTO sessions (chat_id, activity_type) VALUES ($1, 'picnic') RETURNING id",
        chat_id,
    )
    return row["id"]


# "destination" would be the natural example here, but it is a place synonym
# and remember_fact folds those onto "place" — see
# test_place_synonyms_are_stored_under_the_canonical_key. These three test
# generic fact storage, so they use a key with no such reader.
async def test_remember_and_get_fact(db_pool):
    session_id = await _new_session(db_pool)

    await core.remember_fact(db_pool, session_id, "activity", "Hanania meadow")
    facts = await core.get_facts(db_pool, session_id, key="activity")

    assert facts["facts"]["activity"] == "Hanania meadow"


async def test_get_facts_returns_latest_value_when_updated(db_pool):
    session_id = await _new_session(db_pool)

    await core.remember_fact(db_pool, session_id, "activity", "First place")
    await core.remember_fact(db_pool, session_id, "activity", "Second place")
    facts = await core.get_facts(db_pool, session_id, key="activity")

    assert facts["facts"]["activity"] == "Second place"


async def test_get_facts_without_key_returns_all_latest(db_pool):
    session_id = await _new_session(db_pool)

    await core.remember_fact(db_pool, session_id, "activity", "Hanania meadow")
    await core.remember_fact(db_pool, session_id, "headcount", "12")
    facts = await core.get_facts(db_pool, session_id)

    assert facts["facts"] == {"activity": "Hanania meadow", "headcount": "12"}


async def test_get_facts_missing_key_returns_empty(db_pool):
    session_id = await _new_session(db_pool)

    facts = await core.get_facts(db_pool, session_id, key="nope")

    assert facts["facts"] == {}


from unittest.mock import AsyncMock


async def test_propose_confirmation_creates_pending_row(db_pool):
    session_id = await _new_session(db_pool)

    result = await core.propose_confirmation(
        db_pool, chat_id=1, session_id=session_id,
        action_type="list_remove_item", action_params={"name": "tomatoes"},
    )

    assert result["status"] == "pending_confirmation"
    row = await db_pool.fetchrow(
        "SELECT * FROM pending_confirmations WHERE id = $1", result["confirmation_id"]
    )
    assert row["status"] == "pending"
    assert row["action_params"]["name"] == "tomatoes"


async def test_get_pending_confirmation_returns_most_recent_pending(db_pool):
    session_id = await _new_session(db_pool)
    await core.propose_confirmation(
        db_pool, chat_id=1, session_id=session_id,
        action_type="list_remove_item", action_params={"name": "old"},
    )
    second = await core.propose_confirmation(
        db_pool, chat_id=1, session_id=session_id,
        action_type="list_remove_item", action_params={"name": "new"},
    )

    found = await core.get_pending_confirmation(db_pool, chat_id=1)

    assert found["id"] == second["confirmation_id"]


async def test_resolve_confirmation_confirmed(db_pool):
    session_id = await _new_session(db_pool)
    proposed = await core.propose_confirmation(
        db_pool, chat_id=1, session_id=session_id,
        action_type="list_remove_item", action_params={"name": "tomatoes"},
    )

    resolved = await core.resolve_confirmation(db_pool, proposed["confirmation_id"], confirmed=True)

    assert resolved["status"] == "confirmed"


async def test_execute_confirmed_action_removes_list_item(db_pool):
    session_id = await _new_session(db_pool)
    await db_pool.execute(
        "INSERT INTO list_items (session_id, name) VALUES ($1, 'tomatoes')", session_id
    )
    proposed = await core.propose_confirmation(
        db_pool, chat_id=1, session_id=session_id,
        action_type="list_remove_item", action_params={"name": "tomatoes"},
    )
    confirmation = await core.resolve_confirmation(db_pool, proposed["confirmation_id"], confirmed=True)

    result = await core.execute_confirmed_action(db_pool, AsyncMock(), confirmation)

    assert result["status"] == "executed"
    remaining = await db_pool.fetch("SELECT * FROM list_items WHERE session_id = $1", session_id)
    assert remaining == []


async def test_execute_confirmed_action_sends_broadcast(db_pool):
    session_id = await _new_session(db_pool)
    proposed = await core.propose_confirmation(
        db_pool, chat_id=1, session_id=session_id,
        action_type="broadcast_message", action_params={"text": "Reminder: bring meat"},
    )
    confirmation = await core.resolve_confirmation(db_pool, proposed["confirmation_id"], confirmed=True)
    bot = AsyncMock()

    result = await core.execute_confirmed_action(db_pool, bot, confirmation)

    assert result["status"] == "executed"
    bot.send_message.assert_awaited_once_with(chat_id=1, text="Reminder: bring meat")


async def test_list_add_and_show(db_pool):
    session_id = await _new_session(db_pool)

    await core.list_add(db_pool, session_id, "tomatoes")
    await core.list_add(db_pool, session_id, "cucumbers")
    shown = await core.list_show(db_pool, session_id)

    names = {i["name"] for i in shown["items"]}
    assert names == {"tomatoes", "cucumbers"}
    assert all(i["status"] == "pending" for i in shown["items"])


async def test_list_check_off_matches_by_name_case_insensitively(db_pool):
    session_id = await _new_session(db_pool)
    await core.list_add(db_pool, session_id, "Cucumbers")

    result = await core.list_check_off(db_pool, session_id, "cucumbers")

    assert result["status"] == "ok"
    shown = await core.list_show(db_pool, session_id)
    assert shown["items"][0]["status"] == "checked"


async def test_list_check_off_missing_item(db_pool):
    session_id = await _new_session(db_pool)

    result = await core.list_check_off(db_pool, session_id, "nonexistent")

    assert result["status"] == "not_found"


async def test_list_remove_item_is_gated_not_immediate(db_pool):
    session_id = await _new_session(db_pool)
    await core.list_add(db_pool, session_id, "tomatoes")

    result = await core.list_remove_item(db_pool, session_id, "tomatoes")

    assert result["status"] == "pending_confirmation"
    shown = await core.list_show(db_pool, session_id)
    assert len(shown["items"]) == 1  # not actually removed yet


async def test_set_participant_inserts_then_updates(db_pool):
    session_id = await _new_session(db_pool)

    await core.set_participant(db_pool, session_id, "Sasha", "unknown", user_id=111)
    await core.set_participant(db_pool, session_id, "Sasha", "confirmed", user_id=111)

    participants = await core.get_participants(db_pool, session_id)
    assert len(participants["participants"]) == 1
    assert participants["participants"][0]["status"] == "confirmed"


async def test_set_participant_without_user_id_matches_by_name(db_pool):
    session_id = await _new_session(db_pool)

    await core.set_participant(db_pool, session_id, "Masha", "unknown")
    await core.set_participant(db_pool, session_id, "masha", "declined")

    participants = await core.get_participants(db_pool, session_id)
    assert len(participants["participants"]) == 1
    assert participants["participants"][0]["status"] == "declined"


async def test_get_participants_returns_both_counts(db_pool):
    session_id = await _new_session(db_pool)
    await db_pool.execute("UPDATE chats SET member_count = 9 WHERE chat_id = 1")
    await core.set_participant(db_pool, session_id, "Sasha", "confirmed", user_id=111)
    await core.set_participant(db_pool, session_id, "Masha", "unknown")

    result = await core.get_participants(db_pool, session_id)

    assert result["chat_member_count"] == 9
    assert result["recorded_count"] == 2


async def test_get_participants_with_no_stored_count_returns_none_not_zero(db_pool):
    """R7's last criterion: an unfetched count must read as unknown, never as
    an empty group — chats.member_count defaults to NULL until the worker's
    group sync has run at least once for this chat."""
    session_id = await _new_session(db_pool)

    result = await core.get_participants(db_pool, session_id)

    assert result["chat_member_count"] is None


async def test_get_participants_recorded_count_matches_participant_rows(db_pool):
    session_id = await _new_session(db_pool)
    for i in range(3):
        await core.set_participant(db_pool, session_id, f"Person{i}", "unknown", user_id=100 + i)

    result = await core.get_participants(db_pool, session_id)

    assert result["recorded_count"] == len(result["participants"]) == 3


async def test_nudge_unconfirmed_dms_only_those_with_known_user_id(db_pool):
    from unittest.mock import AsyncMock

    session_id = await _new_session(db_pool)
    await core.set_participant(db_pool, session_id, "Sasha", "unknown", user_id=111)
    await core.set_participant(db_pool, session_id, "NoTelegram", "unknown")
    await core.set_participant(db_pool, session_id, "Masha", "confirmed", user_id=222)

    telegram_bot = AsyncMock()
    result = await core.nudge_unconfirmed_participants(db_pool, telegram_bot, session_id)

    assert result["nudged"] == ["Sasha"]
    assert result["skipped_no_user_id"] == ["NoTelegram"]
    telegram_bot.send_message.assert_awaited_once()
    assert telegram_bot.send_message.await_args.kwargs["chat_id"] == 111


async def test_reminder_set_persists_to_queue(db_pool):
    session_id = await _new_session(db_pool)

    result = await core.reminder_set(
        db_pool, session_id, message="Bring the grill",
        remind_at="2026-09-01T09:00:00",
    )

    assert result["status"] == "ok"
    row = await db_pool.fetchrow("SELECT * FROM reminders WHERE id = $1", result["reminder_id"])
    assert row["status"] == "pending"
    assert row["message"] == "Bring the grill"


async def test_reminder_cancel_marks_cancelled(db_pool):
    session_id = await _new_session(db_pool)
    created = await core.reminder_set(
        db_pool, session_id, message="x", remind_at="2026-09-01T09:00:00"
    )

    result = await core.reminder_cancel(db_pool, session_id, created["reminder_id"])

    assert result["status"] == "ok"
    row = await db_pool.fetchrow("SELECT status FROM reminders WHERE id = $1", created["reminder_id"])
    assert row["status"] == "cancelled"


async def test_reminder_cancel_missing_id(db_pool):
    result = await core.reminder_cancel(db_pool, 1, 999999)

    assert result["status"] == "not_found"


async def test_broadcast_message_is_gated(db_pool):
    session_id = await _new_session(db_pool)

    result = await core.broadcast_message(db_pool, session_id, text="Heads up everyone")

    assert result["status"] == "pending_confirmation"


def test_build_core_registry_covers_every_core_tool(db_pool):
    from unittest.mock import AsyncMock

    registry = core.build_core_registry(db_pool, AsyncMock())

    assert set(registry) == {
        "remember_fact", "get_facts", "list_add", "list_show", "list_check_off",
        "list_claim", "list_unclaim",
        "list_remove_item", "set_participant", "get_participants",
        "nudge_unconfirmed_participants", "reminder_set", "reminder_list", "reminder_cancel",
        "broadcast_message", "set_timezone", "send_private_message",
    }


# --- regression tests for code-review findings ---

async def test_set_participant_merges_name_mention_with_later_dm_reply(db_pool):
    """R2: someone is named in the group first ('Masha is coming'), then replies
    in DM where their user_id is known. That must stay ONE participant, or
    get_participants reports them twice with conflicting statuses and the nudge
    DMs someone who already confirmed."""
    session_id = await _new_session(db_pool)

    await core.set_participant(db_pool, session_id, "Masha", "unknown")
    await core.set_participant(db_pool, session_id, "Masha", "confirmed", user_id=333)

    participants = (await core.get_participants(db_pool, session_id))["participants"]
    assert len(participants) == 1
    assert participants[0]["status"] == "confirmed"
    assert participants[0]["user_id"] == 333  # backfilled onto the existing row


async def test_nudge_isolates_per_recipient_failures(db_pool):
    """Telegram refuses to DM anyone who never started the bot — the normal case
    for most group members. One refusal must not abort the whole run."""
    from unittest.mock import AsyncMock

    session_id = await _new_session(db_pool)
    await core.set_participant(db_pool, session_id, "Unreachable", "unknown", user_id=111)
    await core.set_participant(db_pool, session_id, "Reachable", "unknown", user_id=222)

    telegram_bot = AsyncMock()
    telegram_bot.send_message = AsyncMock(
        side_effect=[Exception("Forbidden: bot can't initiate conversation with a user"), None]
    )

    result = await core.nudge_unconfirmed_participants(db_pool, telegram_bot, session_id)

    assert result["nudged"] == ["Reachable"]
    assert result["failed_to_reach"] == ["Unreachable"]


async def test_send_private_message_dms_the_asker_not_the_group(db_pool):
    """R5: the recipient is whoever asked, never the group chat_id — proven
    by asserting the DM lands on chat_id=<the asker's user id>, not on any
    group id."""
    from unittest.mock import AsyncMock

    session_id = await _new_session(db_pool, chat_id=-100)
    telegram_bot = AsyncMock()

    result = await core.send_private_message(
        db_pool, telegram_bot, session_id, current_user_id=555, text="Вот список: помидоры"
    )

    assert result == {"status": "ok"}
    telegram_bot.send_message.assert_awaited_once_with(chat_id=555, text="Вот список: помидоры")


async def test_send_private_message_forbidden_returns_cannot_reach(db_pool):
    """Telegram refuses to DM anyone who never started a chat with the bot —
    the normal state for most group members, not an edge case. Must not
    raise: a failed DM must not take down the turn that produced it."""
    from unittest.mock import AsyncMock

    from telegram.error import Forbidden

    session_id = await _new_session(db_pool)
    telegram_bot = AsyncMock()
    telegram_bot.send_message = AsyncMock(side_effect=Forbidden("bot can't initiate conversation with a user"))

    result = await core.send_private_message(
        db_pool, telegram_bot, session_id, current_user_id=555, text="Вот список"
    )

    assert result == {"status": "cannot_reach", "detail": "the user has never started a chat with the bot"}


async def test_send_private_message_other_failure_returns_failed(db_pool):
    from unittest.mock import AsyncMock

    session_id = await _new_session(db_pool)
    telegram_bot = AsyncMock()
    telegram_bot.send_message = AsyncMock(side_effect=RuntimeError("network blip"))

    result = await core.send_private_message(
        db_pool, telegram_bot, session_id, current_user_id=555, text="Вот список"
    )

    assert result == {"status": "failed", "detail": "network blip"}


async def test_send_private_message_declaration_exposes_only_text():
    """The model must never be able to choose the recipient — session_id and
    current_user_id are bound by the router, so the declaration the model
    sees has to omit both, exactly as chat_id is omitted everywhere else."""
    declared = next(
        fn for fn in schema.ALL_TOOLS.function_declarations if fn.name == "send_private_message"
    )

    assert set(declared.parameters.properties) == {"text"}


async def test_confirmed_action_cannot_be_executed_twice(db_pool):
    """R10: two 'yes' messages (or one redelivered update) must not run the
    gated action twice."""
    from unittest.mock import AsyncMock

    session_id = await _new_session(db_pool)
    proposed = await core.broadcast_message(db_pool, session_id, text="Heads up everyone")
    telegram_bot = AsyncMock()

    first = await core.resolve_confirmation(db_pool, proposed["confirmation_id"], confirmed=True)
    await core.execute_confirmed_action(db_pool, telegram_bot, first)
    second = await core.resolve_confirmation(db_pool, proposed["confirmation_id"], confirmed=True)

    assert second is None  # already resolved
    assert await core.execute_confirmed_action(db_pool, telegram_bot, second) == {"status": "already_resolved"}
    assert telegram_bot.send_message.await_count == 1


async def test_execute_confirmed_action_refuses_an_unconfirmed_row(db_pool):
    from unittest.mock import AsyncMock

    session_id = await _new_session(db_pool)
    proposed = await core.broadcast_message(db_pool, session_id, text="nope")
    rejected = await core.resolve_confirmation(db_pool, proposed["confirmation_id"], confirmed=False)
    telegram_bot = AsyncMock()

    assert await core.execute_confirmed_action(db_pool, telegram_bot, rejected) == {"status": "not_confirmed"}
    telegram_bot.send_message.assert_not_awaited()


async def test_reminder_cancel_cannot_touch_another_sessions_reminder(db_pool):
    """reminder_id is a global BIGSERIAL supplied by the model; an unscoped
    cancel could kill another chat's reminder."""
    mine = await _new_session(db_pool, chat_id=10)
    theirs = await _new_session(db_pool, chat_id=20)
    created = await core.reminder_set(
        db_pool, theirs, message="their reminder", remind_at="2026-09-01T09:00:00"
    )

    result = await core.reminder_cancel(db_pool, mine, created["reminder_id"])

    assert result == {"status": "not_found"}
    row = await db_pool.fetchrow("SELECT status FROM reminders WHERE id = $1", created["reminder_id"])
    assert row["status"] == "pending"


async def test_reminder_and_broadcast_derive_chat_id_from_the_session(db_pool):
    """chat_id is not model-supplied, so it always matches the session's chat."""
    session_id = await _new_session(db_pool, chat_id=77)

    created = await core.reminder_set(db_pool, session_id, message="x", remind_at="2026-09-01T09:00:00")
    proposed = await core.broadcast_message(db_pool, session_id, text="y")

    reminder = await db_pool.fetchrow("SELECT chat_id FROM reminders WHERE id = $1", created["reminder_id"])
    confirmation = await db_pool.fetchrow(
        "SELECT chat_id FROM pending_confirmations WHERE id = $1", proposed["confirmation_id"]
    )
    assert reminder["chat_id"] == 77
    assert confirmation["chat_id"] == 77


async def test_list_check_off_distinguishes_already_checked_from_absent(db_pool):
    """R1: two people both saying 'I got the cucumbers' shouldn't be told
    cucumbers aren't on the list."""
    session_id = await _new_session(db_pool)
    await core.list_add(db_pool, session_id, "cucumbers")
    await core.list_check_off(db_pool, session_id, "cucumbers")

    assert await core.list_check_off(db_pool, session_id, "cucumbers") == {"status": "already_checked"}
    assert (await core.list_check_off(db_pool, session_id, "never added"))["status"] == "not_found"


async def test_adding_the_same_item_twice_keeps_one_row(db_pool):
    """A shopping list with "огурцы" twice is never what anyone meant. Two
    people both asking for milk, or a model re-adding an item it just added
    (observed live on gpt-oss-120b), must not split the list."""
    session_id = await _new_session(db_pool)

    first = await core.list_add(db_pool, session_id, "помидоры")
    second = await core.list_add(db_pool, session_id, "Помидоры")

    assert first["status"] == "ok"
    assert second["status"] == "already_present"
    assert second["item_id"] == first["item_id"]
    assert len((await core.list_show(db_pool, session_id))["items"]) == 1


async def test_re_adding_a_checked_item_says_it_is_already_done(db_pool):
    """The caller needs to distinguish "already on the list" from "already
    bought" to answer honestly."""
    session_id = await _new_session(db_pool)
    await core.list_add(db_pool, session_id, "огурцы")
    await core.list_check_off(db_pool, session_id, "огурцы")

    again = await core.list_add(db_pool, session_id, "огурцы")

    assert again["status"] == "already_present"
    assert again["item_status"] == "checked"


async def test_removing_an_item_removes_it(db_pool):
    from unittest.mock import AsyncMock

    session_id = await _new_session(db_pool)
    await core.list_add(db_pool, session_id, "tomatoes")

    proposed = await core.list_remove_item(db_pool, session_id, "tomatoes")
    confirmation = await core.resolve_confirmation(db_pool, proposed["confirmation_id"], confirmed=True)
    await core.execute_confirmed_action(db_pool, AsyncMock(), confirmation)

    assert (await core.list_show(db_pool, session_id))["items"] == []


async def test_tools_report_unknown_session_instead_of_crashing(db_pool):
    """session_id comes from the model; a stale or hallucinated one must give a
    usable result, not a TypeError on None."""
    from unittest.mock import AsyncMock

    assert await core.list_remove_item(db_pool, 999999, "x") == {"status": "unknown_session"}
    assert await core.broadcast_message(db_pool, 999999, text="x") == {"status": "unknown_session"}
    assert (await core.reminder_set(db_pool, 999999, message="x", remind_at="2026-09-01T09:00:00")
            == {"status": "unknown_session"})
    assert await core.nudge_unconfirmed_participants(db_pool, AsyncMock(), 999999) == {"status": "unknown_session"}


async def test_reminder_set_rejects_an_unparseable_datetime(db_pool):
    session_id = await _new_session(db_pool)

    result = await core.reminder_set(db_pool, session_id, message="x", remind_at="next tuesday-ish")

    assert result["status"] == "bad_datetime"


async def test_confirmation_prompt_reads_as_a_sentence(db_pool):
    """message_for_user is relayed verbatim into the group chat."""
    session_id = await _new_session(db_pool)

    proposed = await core.list_remove_item(db_pool, session_id, "tomatoes")

    assert "{" not in proposed["message_for_user"]
    assert "tomatoes" in proposed["message_for_user"]


def test_declared_parameters_match_the_bound_signatures(db_pool):
    """Mirrors test_tools_composed.py's guard of the same name: the model can
    only pass what the declaration advertises, so a drifted declaration is
    either a TypeError at call time or a chat_id the model gets to choose.

    send_private_message and set_timezone are the exceptions: current_user_id
    is bound by the router from message context, never by the model (see
    bot/router.py's _bind_session_context and _CURRENT_USER_BOUND_TOOLS) — the
    same reasoning that keeps chat_id off every tool's declaration. One picks
    the recipient of a DM; the other, with whose='me', picks whose timezone
    moves, and with it every reminder addressed to them. The registry here is
    the *unbound* one, so the real signatures still carry both; this map is
    what excuses that gap from the equality check below.
    """
    from unittest.mock import AsyncMock

    registry = core.build_core_registry(db_pool, AsyncMock())
    declared = {
        fn.name: set(fn.parameters.properties)
        for fn in schema.ALL_TOOLS.function_declarations
        if fn.name in registry
    }
    router_bound_extra = {
        "send_private_message": {"session_id", "current_user_id"},
        "set_timezone": {"current_user_id"},
    }

    for name, tool in registry.items():
        bound = set(inspect.signature(tool).parameters)
        expected = declared[name] | router_bound_extra.get(name, set())
        assert bound == expected, f"{name}: declared {declared[name]}, accepts {bound}"


async def test_reminder_set_with_a_repeat_stores_both_columns(db_pool):
    session_id = await _new_session(db_pool)

    result = await core.reminder_set(
        db_pool, session_id, message="Пей воду", remind_at="2026-09-01T09:00:00",
        repeat_every_minutes=30, repeat_until="2026-09-01T12:00:00",
    )

    assert result["status"] == "ok"
    assert result["repeats_every_minutes"] == 30
    assert result["repeats_until"] == "2026-09-01T12:00:00"
    row = await db_pool.fetchrow("SELECT * FROM reminders WHERE id = $1", result["reminder_id"])
    assert row["repeat_every_minutes"] == 30
    assert row["repeat_until"] is not None


async def test_reminder_set_repeat_without_an_end_schedules_nothing(db_pool):
    """R1's second criterion in mechanical form: the tool refuses so the model
    has to ask, rather than the instruction alone being a suggestion it can
    skip."""
    session_id = await _new_session(db_pool)

    result = await core.reminder_set(
        db_pool, session_id, message="x", remind_at="2026-09-01T09:00:00",
        repeat_every_minutes=30,
    )

    assert result["status"] == "repeat_needs_an_end"
    assert await db_pool.fetch("SELECT id FROM reminders WHERE session_id = $1", session_id) == []


async def test_reminder_set_refuses_an_interval_below_the_floor(db_pool):
    session_id = await _new_session(db_pool)

    result = await core.reminder_set(
        db_pool, session_id, message="x", remind_at="2026-09-01T09:00:00",
        repeat_every_minutes=2, repeat_until="2026-09-01T12:00:00",
    )

    assert result == {"status": "repeat_too_frequent", "minimum_minutes": 5}
    assert await db_pool.fetch("SELECT id FROM reminders WHERE session_id = $1", session_id) == []


async def test_reminder_set_refuses_an_end_already_in_the_past(db_pool):
    session_id = await _new_session(db_pool)

    result = await core.reminder_set(
        db_pool, session_id, message="x", remind_at="2026-09-01T09:00:00",
        repeat_every_minutes=30, repeat_until="2020-01-01T00:00:00",
    )

    assert result["status"] == "repeat_end_in_the_past"
    assert await db_pool.fetch("SELECT id FROM reminders WHERE session_id = $1", session_id) == []


async def test_reminder_set_without_a_repeat_is_unchanged(db_pool):
    session_id = await _new_session(db_pool)

    result = await core.reminder_set(
        db_pool, session_id, message="x", remind_at="2026-09-01T09:00:00",
    )

    assert result["status"] == "ok"
    assert "repeats_every_minutes" not in result
    assert "repeats_until" not in result
    row = await db_pool.fetchrow("SELECT * FROM reminders WHERE id = $1", result["reminder_id"])
    assert row["repeat_every_minutes"] is None
    assert row["repeat_until"] is None


async def test_reminder_list_renders_in_the_chats_own_timezone(db_pool):
    """R2: next delivery time is shown in the chat's own timezone, not UTC."""
    session_id = await _new_session(db_pool, tz="Europe/Moscow")
    created = await core.reminder_set(
        db_pool, session_id, message="Выезжаем", remind_at="2026-09-01T09:00:00",
    )

    result = await core.reminder_list(db_pool, session_id)

    assert len(result["reminders"]) == 1
    entry = result["reminders"][0]
    assert entry["reminder_id"] == created["reminder_id"]
    assert entry["message"] == "Выезжаем"
    assert entry["next_at"] == "2026-09-01T09:00:00"  # the local hour it was asked for, not UTC


async def test_reminder_list_omits_sent_and_cancelled(db_pool):
    session_id = await _new_session(db_pool)
    pending = await core.reminder_set(db_pool, session_id, message="pending", remind_at="2026-09-01T09:00:00")
    sent = await core.reminder_set(db_pool, session_id, message="sent", remind_at="2026-09-01T09:00:00")
    cancelled = await core.reminder_set(db_pool, session_id, message="cancelled", remind_at="2026-09-01T09:00:00")
    await db_pool.execute("UPDATE reminders SET status = 'sent' WHERE id = $1", sent["reminder_id"])
    await core.reminder_cancel(db_pool, session_id, cancelled["reminder_id"])

    result = await core.reminder_list(db_pool, session_id)

    assert [r["reminder_id"] for r in result["reminders"]] == [pending["reminder_id"]]


async def test_reminder_list_empty_when_nothing_scheduled(db_pool):
    """R2's second criterion: the bot must be able to say "nothing scheduled"
    plainly, which only works if the tool distinguishes that from an error."""
    session_id = await _new_session(db_pool)

    result = await core.reminder_list(db_pool, session_id)

    assert result == {"reminders": []}


async def test_reminder_list_shows_group_target_as_gruppa(db_pool):
    session_id = await _new_session(db_pool)
    await core.reminder_set(db_pool, session_id, message="x", remind_at="2026-09-01T09:00:00")

    result = await core.reminder_list(db_pool, session_id)

    assert result["reminders"][0]["target"] == "группа"


async def test_reminder_list_shows_a_known_participants_name(db_pool):
    session_id = await _new_session(db_pool)
    await core.set_participant(db_pool, session_id, "Маша", "confirmed", user_id=333)
    await core.reminder_set(
        db_pool, session_id, message="x", remind_at="2026-09-01T09:00:00", target_user_id=333,
    )

    result = await core.reminder_list(db_pool, session_id)

    assert result["reminders"][0]["target"] == "Маша"


async def test_reminder_list_falls_back_to_the_id_for_an_unknown_target(db_pool):
    """The person isn't in participants — dropping the row would hide a real,
    scheduled reminder; falling back to the id keeps it visible."""
    session_id = await _new_session(db_pool)
    await core.reminder_set(
        db_pool, session_id, message="x", remind_at="2026-09-01T09:00:00", target_user_id=999,
    )

    result = await core.reminder_list(db_pool, session_id)

    assert result["reminders"][0]["target"] == "999"


async def test_reminder_list_includes_repeat_fields(db_pool):
    session_id = await _new_session(db_pool)
    await core.reminder_set(
        db_pool, session_id, message="Пей воду", remind_at="2026-09-01T09:00:00",
        repeat_every_minutes=30, repeat_until="2026-09-01T12:00:00",
    )

    result = await core.reminder_list(db_pool, session_id)

    entry = result["reminders"][0]
    assert entry["repeats_every_minutes"] == 30
    assert entry["repeats_until"] == "2026-09-01T12:00:00"


async def test_reminder_list_non_repeating_has_none_repeat_fields(db_pool):
    session_id = await _new_session(db_pool)
    await core.reminder_set(db_pool, session_id, message="x", remind_at="2026-09-01T09:00:00")

    result = await core.reminder_list(db_pool, session_id)

    entry = result["reminders"][0]
    assert entry["repeats_every_minutes"] is None
    assert entry["repeats_until"] is None


# --- S3: quantity, category and claiming ---


async def test_list_add_with_quantity_and_category(db_pool):
    session_id = await _new_session(db_pool)

    result = await core.list_add(db_pool, session_id, "молоко", amount=2, unit="литр", category="молочка")

    assert result["status"] == "ok"
    row = await db_pool.fetchrow(
        "SELECT amount, unit, category FROM list_items WHERE id = $1", result["item_id"]
    )
    assert (float(row["amount"]), row["unit"]) == (2.0, "литр")
    assert row["category"] == "молочка"


async def test_list_add_without_a_quantity_leaves_it_blank(db_pool):
    session_id = await _new_session(db_pool)

    result = await core.list_add(db_pool, session_id, "картошка")

    assert result["status"] == "ok"
    row = await db_pool.fetchrow("SELECT quantity FROM list_items WHERE id = $1", result["item_id"])
    assert row["quantity"] is None


async def test_list_add_fills_in_a_blank_quantity(db_pool):
    """R4: "возьмите картошки" then "картошки, килограмма два" is adding
    information, not repeating the same request — it must update the row
    rather than reporting already_present."""
    session_id = await _new_session(db_pool)
    first = await core.list_add(db_pool, session_id, "картошка")

    result = await core.list_add(db_pool, session_id, "картошка", amount=2, unit="килограмм")

    assert result["status"] == "updated"
    assert result["item_id"] == first["item_id"]
    assert result["quantity"] == "2 кг"
    # amounts is where writes go now; amount/unit are the legacy read path.
    assert (await core.list_show(db_pool, session_id))["items"][0]["amounts"] == [
        {"amount": 2.0, "unit": "килограмм"}
    ]


async def test_a_second_amount_is_refused_rather_than_guessed_at(db_pool):
    """With "вино, 1 ящ." listed, "добавь бутылку" could mean replace the
    crate or add to it. Guessing either way invents an intention, and a wrong
    quantity nobody can see is wrong. So nothing changes and the caller is
    told what is there, to ask for the total instead."""
    session_id = await _new_session(db_pool)
    await core.list_add(db_pool, session_id, "вино", amount=1, unit="ящик")

    result = await core.list_add(db_pool, session_id, "вино", amount=1, unit="бутылка", relative=True)

    assert result["status"] == "already_present"
    assert result["quantity"] == "1 ящ."
    assert "never added to or subtracted from" in result["ask_user"]
    assert "◻️ вино, 1 ящ." in result["rendered"], "the list must be untouched"


async def test_a_restated_total_replaces_what_was_there(db_pool):
    """The answer to the question above: "ящик и две бутылки" is one total,
    and it replaces rather than adding to what it already says."""
    session_id = await _new_session(db_pool)
    await core.list_add(db_pool, session_id, "вино", amount=1, unit="ящик")

    result = await core.list_add(db_pool, session_id, "вино", amounts=[
        {"amount": 1, "unit": "ящик"}, {"amount": 2, "unit": "бутылка"},
    ])

    assert result["status"] == "updated"
    assert result["quantity"] == "1 ящ. + 2 бут."
    assert result["previous_quantity"] == "1 ящ."


async def test_restating_the_same_amount_changes_nothing(db_pool):
    """Two people asking for the same two kilos is not a correction, and must
    not read as one."""
    session_id = await _new_session(db_pool)
    await core.list_add(db_pool, session_id, "молоко", amount=2, unit="литр")

    result = await core.list_add(db_pool, session_id, "молоко", amount=2, unit="литр")

    assert result["status"] == "already_present"
    assert result["quantity"] == "2 л"
    assert len((await core.list_show(db_pool, session_id))["items"][0]["amounts"]) == 1


async def test_list_claim_sets_claimant(db_pool):
    session_id = await _new_session(db_pool)
    await core.list_add(db_pool, session_id, "хлеб")

    result = await core.list_claim(db_pool, session_id, "хлеб", "Alex", claimed_by_user_id=42)

    assert result["status"] == "ok"
    row = await db_pool.fetchrow(
        "SELECT claimed_by, claimed_by_user_id FROM list_items WHERE session_id = $1 AND lower(name) = 'хлеб'",
        session_id,
    )
    assert row["claimed_by"] == "Alex"
    assert row["claimed_by_user_id"] == 42


async def test_list_claim_missing_item_returns_real_names(db_pool):
    """Mirrors list_check_off's not_found affordance (0001's S6): a model that
    invents a name rather than matching one needs the real names to retry with."""
    session_id = await _new_session(db_pool)
    await core.list_add(db_pool, session_id, "огурцы")

    result = await core.list_claim(db_pool, session_id, "cucumbers", "Alex")

    assert result["status"] == "not_found"
    assert result["items_on_the_list"] == ["огурцы"]


async def test_list_unclaim_clears_claim(db_pool):
    session_id = await _new_session(db_pool)
    await core.list_add(db_pool, session_id, "хлеб")
    await core.list_claim(db_pool, session_id, "хлеб", "Alex")

    result = await core.list_unclaim(db_pool, session_id, "хлеб")

    assert result["status"] == "ok"
    row = await db_pool.fetchrow(
        "SELECT claimed_by, claimed_by_user_id FROM list_items WHERE session_id = $1 AND lower(name) = 'хлеб'",
        session_id,
    )
    assert row["claimed_by"] is None
    assert row["claimed_by_user_id"] is None


async def test_list_unclaim_missing_item_returns_real_names(db_pool):
    session_id = await _new_session(db_pool)
    await core.list_add(db_pool, session_id, "огурцы")

    result = await core.list_unclaim(db_pool, session_id, "cucumbers")

    assert result["status"] == "not_found"
    assert result["items_on_the_list"] == ["огурцы"]


async def test_an_invented_category_lands_in_prochee(db_pool):
    """The category vocabulary is fixed in code (S3): a model-invented category
    must not be rejected, and must not corrupt the sort in Task 3."""
    session_id = await _new_session(db_pool)

    result = await core.list_add(db_pool, session_id, "хлеб", category="выпечка домашняя")

    row = await db_pool.fetchrow("SELECT category FROM list_items WHERE id = $1", result["item_id"])
    assert row["category"] == "прочее"


async def test_list_show_includes_the_rendered_text(db_pool):
    """R4: list_show hands back both the raw rows, for the model to reason
    about, and the rendered plain text it should actually show."""
    session_id = await _new_session(db_pool)
    await core.list_add(db_pool, session_id, "молоко", amount=2, unit="литр", category="молочка")

    shown = await core.list_show(db_pool, session_id)

    assert shown["items"][0]["quantity"] == "2 л"
    assert shown["items"][0]["amounts"] == [{"amount": 2.0, "unit": "литр"}]
    assert shown["items"][0]["category"] == "молочка"
    assert shown["rendered"] == "Ещё не разобрали:\n◻️ молоко, 2 л"


async def test_a_missed_check_off_hands_back_the_real_item_names(db_pool):
    """Observed live: asked to check off "огурцы", the model translated the
    name, added "cucumbers" and checked that off instead — leaving the real
    item outstanding. A bare not_found gives it nothing to correct with."""
    session_id = await _new_session(db_pool)
    await core.list_add(db_pool, session_id, "огурцы")
    await core.list_add(db_pool, session_id, "мясо")

    result = await core.list_check_off(db_pool, session_id, "cucumbers")

    assert result["status"] == "not_found"
    assert set(result["items_on_the_list"]) == {"огурцы", "мясо"}


async def test_list_add_refuses_a_name_that_matches_a_participant(db_pool):
    """Reproduced live: an unaddressed message naming who is coming ("Андрюха
    и Витька тоже придут") let the silent-capture classifier confuse the
    people with things to bring, and list_add happily stored both names as
    bare shopping-list items — no quantity, no owner, just a person's name
    sitting in the middle of the list. A name already tracked as a
    participant in this session is refused rather than trusted, regardless
    of which caller passed it in."""
    session_id = await _new_session(db_pool)
    await core.set_participant(db_pool, session_id, "Андрюха", "confirmed", user_id=1)
    await core.set_participant(db_pool, session_id, "Витька", "unknown", user_id=2)

    r1 = await core.list_add(db_pool, session_id, "Андрюха")
    r2 = await core.list_add(db_pool, session_id, "Витька")

    assert r1 == {"status": "looks_like_a_participant", "detail": "Андрюха"}
    assert r2 == {"status": "looks_like_a_participant", "detail": "Витька"}
    assert (await core.list_show(db_pool, session_id))["items"] == []


async def test_list_add_participant_name_check_is_case_insensitive(db_pool):
    session_id = await _new_session(db_pool)
    await core.set_participant(db_pool, session_id, "андрюха", "confirmed", user_id=1)

    result = await core.list_add(db_pool, session_id, "АНДРЮХА")

    assert result["status"] == "looks_like_a_participant"


async def test_list_add_still_works_for_ordinary_items(db_pool):
    """The guard must not catch anything but an actual name match."""
    session_id = await _new_session(db_pool)
    await core.set_participant(db_pool, session_id, "Alex", "confirmed", user_id=1)

    result = await core.list_add(db_pool, session_id, "арбуз")

    assert result["status"] == "ok"


async def test_an_item_added_before_the_name_becomes_a_participant_is_not_removed(db_pool):
    """The guard only stops a NEW add from being mistaken for a person; it
    must never retroactively delete an item someone already legitimately
    added, even if a later participant happens to share its name."""
    session_id = await _new_session(db_pool)
    await core.list_add(db_pool, session_id, "Персик")

    await core.set_participant(db_pool, session_id, "Персик", "unknown", user_id=9)

    items = [i["name"] for i in (await core.list_show(db_pool, session_id))["items"]]
    assert items == ["Персик"]



async def test_set_participant_accepts_maybe_as_a_distinct_status(db_pool):
    """"maybe" (a hedged reply) is a genuinely separate state from "unknown"
    (nobody has answered at all) — without a distinct value in the schema the
    ❓/◻️ icon distinction the participant renderer draws would be impossible."""
    session_id = await _new_session(db_pool)

    await core.set_participant(db_pool, session_id, "Света", "maybe", user_id=333)

    status = await db_pool.fetchval(
        "SELECT status FROM participants WHERE session_id = $1 AND user_id = 333", session_id
    )
    assert status == "maybe"


async def test_get_participants_includes_the_rendered_roster(db_pool):
    session_id = await _new_session(db_pool)
    await core.set_participant(db_pool, session_id, "Андрюха", "confirmed", user_id=1)
    await core.set_participant(db_pool, session_id, "Витька", "maybe", user_id=2)

    result = await core.get_participants(db_pool, session_id)

    assert result["rendered"] == "✅ Андрюха\n❓ Витька"


async def test_participant_icon_updates_live_when_status_changes(db_pool):
    """End to end, against a real database: the rendered icon for one person
    changes the moment their status does, with no separate step to keep it
    in sync."""
    session_id = await _new_session(db_pool)
    await core.set_participant(db_pool, session_id, "Витька", "unknown", user_id=2)

    before = (await core.get_participants(db_pool, session_id))["rendered"]
    await core.set_participant(db_pool, session_id, "Витька", "confirmed", user_id=2)
    after = (await core.get_participants(db_pool, session_id))["rendered"]

    assert before == "◻️ Витька"
    assert after == "✅ Витька"


async def test_place_synonyms_are_stored_under_the_canonical_key(db_pool):
    """The live failure: event_status reads facts under the exact key "place",
    but remember_fact takes any key the model invents. In one real session it
    chose "destination", then "event_name" — so nothing ever wrote "place" and
    the 📍 line stayed blank for the session's whole life while the answer sat
    in the table under another name.
    """
    session_id = await _new_session(db_pool)

    for key in ("destination", "event_name", "location", "Место", "МЕСТО_ВСТРЕЧИ"):
        result = await core.remember_fact(db_pool, session_id, key, f"via {key}")
        assert result["key"] == "place", f"{key} was not folded onto place"

    facts = (await core.get_facts(db_pool, session_id, key="place"))["facts"]
    assert facts["place"] == "via МЕСТО_ВСТРЕЧИ", "the latest write should win"


async def test_an_ordinary_fact_key_is_left_exactly_as_written(db_pool):
    """Only place has a reader that depends on an exact name. Folding anything
    else would silently merge unrelated facts under one key."""
    session_id = await _new_session(db_pool)

    await core.remember_fact(db_pool, session_id, "пиво", "каждый приносит на себя")
    await core.remember_fact(db_pool, session_id, "allergies", "орехи")

    facts = (await core.get_facts(db_pool, session_id))["facts"]
    assert facts["пиво"] == "каждый приносит на себя"
    assert facts["allergies"] == "орехи"


async def test_adding_an_item_returns_the_updated_list_as_confirmation(db_pool):
    """Asked to add three things the bot answered nothing at all: list_add
    returned only {"status": "ok"}, the model decided no reply was needed, and
    the person could not tell whether anything had been recorded. The rendered
    block is the confirmation, and it also reaches tool_loop's verbatim guard
    so it gets relayed rather than paraphrased."""
    session_id = await _new_session(db_pool)

    added = await core.list_add(db_pool, session_id, "мясо", amount=2, unit="килограмм")

    assert added["status"] == "ok"
    assert "◻️ мясо, 2 кг" in added["rendered"]


async def test_every_list_add_outcome_carries_the_list(db_pool):
    """A confirmation is just as necessary when nothing changed: "already on
    the list" is only believable if it shows the list."""
    session_id = await _new_session(db_pool)
    await core.list_add(db_pool, session_id, "пиво")

    again = await core.list_add(db_pool, session_id, "пиво")
    updated = await core.list_add(db_pool, session_id, "пиво", amount=6, unit="бутылка")

    assert again["status"] == "already_present"
    assert "◻️ пиво" in again["rendered"]
    assert updated["status"] == "updated"
    assert "◻️ пиво, 6 бут." in updated["rendered"]


async def test_a_refused_participant_name_returns_no_list(db_pool):
    """The refusal path adds nothing, so there is no change to confirm — and
    returning a block here would have tool_loop relay the whole list in
    answer to a message that was about a person."""
    session_id = await _new_session(db_pool)
    await core.set_participant(db_pool, session_id, "Витька", "unknown", user_id=7)

    refused = await core.list_add(db_pool, session_id, "витька")

    assert refused["status"] == "looks_like_a_participant"
    assert "rendered" not in refused


async def test_quantity_in_the_name_does_not_create_a_second_row(db_pool):
    """The live duplicate. One message reached both the silent-capture and the
    addressed path; each extracted the amount differently, so "мяса, 2 кг" and
    "2 кг мяса" ended up as two rows for the same meat."""
    session_id = await _new_session(db_pool)

    first = await core.list_add(db_pool, session_id, "мяса", amount=2, unit="килограмм")
    second = await core.list_add(db_pool, session_id, "2 кг мяса", amount=2, unit="килограмм")

    assert first["status"] == "ok"
    # Not "updated", and not 4 кг: one message reaching two paths must not
    # double the amount.
    assert second["status"] == "already_present"
    assert second["quantity"] == "2 кг"
    rows = await db_pool.fetchval(
        "SELECT count(*) FROM list_items WHERE session_id = $1", session_id
    )
    assert rows == 1


async def test_the_stored_name_has_no_amount_and_is_nominative(db_pool):
    """The amount already has its own column, and the case someone happened to
    speak in is not part of the item's identity."""
    session_id = await _new_session(db_pool)

    await core.list_add(db_pool, session_id, "килограмм помидоров", amount=1, unit="килограмм")
    await core.list_add(db_pool, session_id, "2 пачки макарон", amount=2, unit="пачка")

    stored = [r["name"] for r in await db_pool.fetch(
        "SELECT name FROM list_items WHERE session_id = $1 ORDER BY id", session_id
    )]
    assert stored == ["помидоры", "макароны"]


async def test_the_same_item_in_another_case_is_not_added_twice(db_pool):
    """"Добавь мяса" then "добавь мясо" is one item, not two."""
    session_id = await _new_session(db_pool)

    await core.list_add(db_pool, session_id, "мяса", amount=2, unit="килограмм")
    again = await core.list_add(db_pool, session_id, "мясо")

    assert again["status"] == "already_present"
    assert await db_pool.fetchval(
        "SELECT count(*) FROM list_items WHERE session_id = $1", session_id
    ) == 1


async def test_checking_off_works_across_cases(db_pool):
    """Stored as "мясо"; someone says they got "мяса". Without the same
    normalisation on lookup this reports the item absent and invites a
    duplicate — the asymmetry that made rewriting names dangerous before."""
    session_id = await _new_session(db_pool)
    await core.list_add(db_pool, session_id, "мясо")

    result = await core.list_check_off(db_pool, session_id, "мяса")

    assert result["status"] == "ok"


async def test_word_order_alone_does_not_create_a_second_row(db_pool):
    session_id = await _new_session(db_pool)

    await core.list_add(db_pool, session_id, "красное вино")
    again = await core.list_add(db_pool, session_id, "вино красное")

    assert again["status"] == "already_present"


async def test_an_item_actually_called_a_quantity_word_survives(db_pool):
    """Stripping must never empty a name: a group asking for "бутылка" gets a
    bottle, not a row called ""."""
    session_id = await _new_session(db_pool)

    added = await core.list_add(db_pool, session_id, "бутылка")

    assert added["status"] == "ok"
    stored = await db_pool.fetchval(
        "SELECT name FROM list_items WHERE session_id = $1", session_id
    )
    assert stored == "бутылка"


async def test_checking_off_finds_the_item_despite_the_amount(db_pool):
    """list_add stores "мяса"; someone then says they got "2 кг мяса". An
    exact-name lookup would call that item absent and invite a duplicate."""
    session_id = await _new_session(db_pool)
    await core.list_add(db_pool, session_id, "мяса", amount=2, unit="килограмм")

    result = await core.list_check_off(db_pool, session_id, "2 кг мяса")

    assert result["status"] == "ok"


async def test_claiming_finds_the_item_despite_word_order(db_pool):
    session_id = await _new_session(db_pool)
    await core.list_add(db_pool, session_id, "красное вино")

    result = await core.list_claim(db_pool, session_id, "вино красное", claimed_by="Alex")

    assert result["status"] == "ok"


async def test_the_same_amount_said_three_ways_reads_the_same(db_pool):
    """The point of the split. The free-text quantity column stored "2 кг",
    "одну бутылку" and "полкило" exactly as spoken, so one list showed three
    different shapes for the same kind of information."""
    session_id = await _new_session(db_pool)

    await core.list_add(db_pool, session_id, "мясо", amount=0.5, unit="килограмм")
    await core.list_add(db_pool, session_id, "чай", amount=1, unit="бутылка")
    await core.list_add(db_pool, session_id, "помидоры", amount=2, unit="килограмм")

    rendered = (await core.list_show(db_pool, session_id))["rendered"]

    assert rendered == (
        "Ещё не разобрали:\n"
        "◻️ мясо, 0.5 кг\n"
        "◻️ помидоры, 2 кг\n"
        "◻️ чай, 1 бут."
    )


async def test_an_item_with_no_amount_stays_bare(db_pool):
    """Nobody said how much, so nothing is shown — not a "1 штука" the list
    invented."""
    session_id = await _new_session(db_pool)

    await core.list_add(db_pool, session_id, "пиво")

    assert (await core.list_show(db_pool, session_id))["rendered"] == "Ещё не разобрали:\n◻️ пиво"


async def test_an_unparseable_amount_leaves_the_item_bare(db_pool):
    """The model is asked for a number and does not always send one. A bare
    unit or an empty string must not crash the insert or record a zero."""
    session_id = await _new_session(db_pool)

    added = await core.list_add(db_pool, session_id, "хлеб", amount="", unit="штука")

    assert added["status"] == "ok"
    row = await db_pool.fetchrow(
        "SELECT amount, unit FROM list_items WHERE id = $1", added["item_id"]
    )
    assert (row["amount"], row["unit"]) == (None, None)


async def test_an_older_spelling_is_repaired_when_the_item_comes_up_again(db_pool):
    """Rows written before names were canonicalised keep whatever reached
    them. One live list had "одна бутылка чай" — it matches "чай" by key, so
    it was found and its amount updated, but the nonsense name stayed and
    nothing would ever have repaired it."""
    session_id = await _new_session(db_pool)
    await db_pool.execute(
        "INSERT INTO list_items (session_id, name) VALUES ($1, 'одна бутылка чай')",
        session_id,
    )

    result = await core.list_add(db_pool, session_id, "чай", amount=1, unit="бутылка")

    assert result["status"] == "updated"
    names = [r["name"] for r in await db_pool.fetch(
        "SELECT name FROM list_items WHERE session_id = $1", session_id
    )]
    assert names == ["чай"], "the older spelling should have been repaired, not duplicated"
    assert "◻️ чай, 1 бут." in result["rendered"]


async def test_the_same_fruit_in_the_singular_is_not_added_again(db_pool):
    """Live: the list said "бананы" and the model sent name="банан", so the
    same fruit ended up on two rows."""
    session_id = await _new_session(db_pool)
    await core.list_add(db_pool, session_id, "бананы")

    again = await core.list_add(db_pool, session_id, "банан", amount=1, unit="килограмм")

    assert again["status"] == "updated"
    assert await db_pool.fetchval(
        "SELECT count(*) FROM list_items WHERE session_id = $1", session_id
    ) == 1


async def test_a_crate_becomes_an_amount(db_pool):
    """Live: "ящик вина" reached the list as an item called "ящик вино" —
    there was no crate in the unit vocabulary for it to go to."""
    session_id = await _new_session(db_pool)

    added = await core.list_add(db_pool, session_id, "ящик вина", amount=1, unit="ящик")

    assert added["status"] == "ok"
    assert "◻️ вино, 1 ящ." in added["rendered"]


async def test_two_older_spellings_are_merged_not_collided(db_pool):
    """Reproduced live and it crashed. A list held "банан" and "бананы" from
    before matching was by lemma; mentioning bananas found the first, tried to
    rename it to "бананы", and hit
    UniqueViolationError: duplicate key value violates constraint
    "one_item_name_per_session".
    """
    session_id = await _new_session(db_pool)
    await db_pool.execute(
        "INSERT INTO list_items (session_id, name, amount, unit) "
        "VALUES ($1, 'банан', 1, 'килограмм'), ($1, 'бананы', NULL, NULL)",
        session_id,
    )

    result = await core.list_add(db_pool, session_id, "бананы")

    assert result["status"] in {"ok", "updated", "already_present"}
    names = [r["name"] for r in await db_pool.fetch(
        "SELECT name FROM list_items WHERE session_id = $1", session_id
    )]
    assert names == ["бананы"]


async def test_merging_keeps_what_the_other_row_knew(db_pool):
    """The surviving row is whichever already had the canonical name, which
    is not necessarily the one carrying the amount or the claim. Dropping
    those would lose something a person actually said."""
    session_id = await _new_session(db_pool)
    await db_pool.execute(
        "INSERT INTO list_items (session_id, name, amount, unit, claimed_by) "
        "VALUES ($1, 'банан', 2, 'килограмм', 'Alex'), ($1, 'бананы', NULL, NULL, NULL)",
        session_id,
    )

    result = await core.list_add(db_pool, session_id, "бананы")

    assert "◻️" not in result["rendered"], "a claimed item should render as taken"
    row = await db_pool.fetchrow(
        "SELECT name, amount, unit, claimed_by FROM list_items WHERE session_id = $1", session_id
    )
    assert (row["name"], float(row["amount"]), row["unit"], row["claimed_by"]) == (
        "бананы", 2.0, "килограмм", "Alex",
    )


async def test_an_older_single_amount_row_is_read_and_replaced(db_pool):
    """Rows written before the amounts column carry one pair in amount/unit.
    Both the refusal and the restatement have to see it."""
    session_id = await _new_session(db_pool)
    await db_pool.execute(
        "INSERT INTO list_items (session_id, name, amount, unit) VALUES ($1, 'вино', 1, 'ящик')",
        session_id,
    )

    refused = await core.list_add(db_pool, session_id, "вино", amount=1, unit="бутылка", relative=True)
    assert refused["status"] == "already_present"
    assert refused["quantity"] == "1 ящ."

    result = await core.list_add(db_pool, session_id, "вино", amounts=[
        {"amount": 1, "unit": "ящик"}, {"amount": 2, "unit": "бутылка"},
    ])
    assert result["quantity"] == "1 ящ. + 2 бут."


def test_a_nested_array_parameter_survives_the_openai_translation():
    """Groq gets its tools through bot/ai/tool_schema_openai.py, which used to
    render only type and description. list_add's amounts is the first
    array-of-objects declared, and came out as a bare {"type": "array"} with
    no item schema — leaving the model to guess what belongs inside."""
    from bot.ai.tool_schema_openai import openai_tools

    declared = next(
        t["function"] for t in openai_tools() if t["function"]["name"] == "list_add"
    )
    amounts = declared["parameters"]["properties"]["amounts"]

    assert amounts["type"] == "array"
    assert amounts["items"]["type"] == "object"
    assert set(amounts["items"]["properties"]) == {"amount", "unit"}
    assert amounts["items"]["required"] == ["amount", "unit"]


async def test_an_item_with_no_amount_asks_how_much_instead_of_stonewalling(db_pool):
    """Live: "Добавь ещё водку" against a bare "водка" row came back
    already_present, and the bot answered "Водка уже есть в списке." — true,
    useless, and it ignored the word that mattered. There is nothing to add to
    and nothing to add, so the only useful move is to ask."""
    session_id = await _new_session(db_pool)
    await core.list_add(db_pool, session_id, "водка")

    again = await core.list_add(db_pool, session_id, "водка", relative=True)

    assert again["status"] == "already_present"
    assert again["quantity"] is None
    assert "no amount recorded" in again["ask_user"]
    assert "never added to or subtracted from" in again["ask_user"]


async def test_the_refusal_names_what_is_already_there(db_pool):
    """The reply has to be actionable: "уже есть" alone leaves the person
    guessing what to say instead."""
    session_id = await _new_session(db_pool)
    await core.list_add(db_pool, session_id, "пиво", amount=1, unit="ящик")

    again = await core.list_add(db_pool, session_id, "пиво", relative=True)

    assert again["status"] == "already_present"
    assert again["quantity"] == "1 ящ."
    assert "as 1 ящ." in again["ask_user"]


async def test_a_perfectly_good_name_is_not_rewritten(db_pool):
    """Live: the list said "бананы", the model sent "банан" for the same
    fruit, and the repair renamed the row — rewriting a group's plural into
    the model's singular. Matching is by lemma precisely so the two can be
    the same item without either having to win."""
    session_id = await _new_session(db_pool)
    await core.list_add(db_pool, session_id, "бананы", amount=1, unit="килограмм")

    await core.list_add(db_pool, session_id, "банан", amount=1, unit="килограмм")

    names = [r["name"] for r in await db_pool.fetch(
        "SELECT name FROM list_items WHERE session_id = $1", session_id
    )]
    assert names == ["бананы"]


async def test_a_name_nothing_would_produce_today_is_still_repaired(db_pool):
    """The repair's actual job. "одна бутылка чай" is not a name anybody
    would write now — it predates quantity being split out."""
    session_id = await _new_session(db_pool)
    await db_pool.execute(
        "INSERT INTO list_items (session_id, name) VALUES ($1, 'одна бутылка чай')",
        session_id,
    )

    await core.list_add(db_pool, session_id, "чай")

    names = [r["name"] for r in await db_pool.fetch(
        "SELECT name FROM list_items WHERE session_id = $1", session_id
    )]
    assert names == ["чай"]


async def test_a_stated_total_applies_without_any_flag(db_pool):
    """The failure this default was inverted for. Told "Должно быть 700 г
    бананов" the model called with amount/unit rather than the restatement
    parameter, was refused, and the person could not change the number at
    all. Both mistakes are possible; they are not equal — setting an amount
    when someone meant "ещё" is recoverable by saying the total, refusing a
    total is a dead end."""
    session_id = await _new_session(db_pool)
    await core.list_add(db_pool, session_id, "бананы", amount=1, unit="килограмм")

    result = await core.list_add(db_pool, session_id, "бананы", amount=700, unit="грамм")

    assert result["status"] == "updated"
    assert result["quantity"] == "700 г"
    assert result["previous_quantity"] == "1 кг"


async def test_a_relative_change_is_still_refused(db_pool):
    """The flag is what preserves the behaviour that was asked for."""
    session_id = await _new_session(db_pool)
    await core.list_add(db_pool, session_id, "пиво", amount=1, unit="ящик")

    result = await core.list_add(
        db_pool, session_id, "пиво", amount=1, unit="бутылка", relative=True
    )

    assert result["status"] == "already_present"
    assert result["quantity"] == "1 ящ."


async def test_a_relative_change_to_something_with_no_amount_just_sets_it(db_pool):
    """There was nothing to add to, so there is nothing to be confused about."""
    session_id = await _new_session(db_pool)
    await core.list_add(db_pool, session_id, "водка")

    result = await core.list_add(
        db_pool, session_id, "водка", amount=2, unit="бутылка", relative=True
    )

    assert result["status"] == "updated"
    assert result["quantity"] == "2 бут."


async def test_taking_some_away_is_refused_the_same_as_adding(db_pool):
    """Live: "Хлеб. Убери один" against "хлеб, 2 шт." did nothing at all.
    The flag was called "adding" and covered only more, so nothing in the
    tool described removal — the model found no action that fit, read the
    list, and answered with the status report instead."""
    session_id = await _new_session(db_pool)
    await core.list_add(db_pool, session_id, "хлеб", amount=2, unit="штука")

    result = await core.list_add(
        db_pool, session_id, "хлеб", amount=1, unit="штука", relative=True
    )

    assert result["status"] == "already_present"
    assert result["quantity"] == "2 шт."
    assert "neither add nor take away" in result["ask_user"]
    assert "◻️ хлеб, 2 шт." in result["rendered"], "the list must be untouched"


async def test_the_answer_to_that_question_applies(db_pool):
    """"сколько всего должно остаться" -> "один" is an absolute amount."""
    session_id = await _new_session(db_pool)
    await core.list_add(db_pool, session_id, "хлеб", amount=2, unit="штука")

    result = await core.list_add(db_pool, session_id, "хлеб", amount=1, unit="штука")

    assert result["status"] == "updated"
    assert result["quantity"] == "1 шт."
    assert result["previous_quantity"] == "2 шт."


async def test_a_leftover_count_is_an_absolute_amount(db_pool):
    """"Хлеба осталось 2" says how much there is, not how much to change by."""
    session_id = await _new_session(db_pool)
    await core.list_add(db_pool, session_id, "хлеб", amount=5, unit="штука")

    result = await core.list_add(db_pool, session_id, "хлеб", amount=2, unit="штука")

    assert result["status"] == "updated"
    assert result["quantity"] == "2 шт."


async def test_replacing_an_amount_says_what_it_replaced(db_pool):
    """Live: "добавь бутылку пива" against "2 бут." wrote "1 бут." and the
    reply was the whole list, with nothing naming the bottle that vanished.
    A replacement that overwrote something has to name both values."""
    session_id = await _new_session(db_pool)
    await core.list_add(db_pool, session_id, "пиво", amount=2, unit="бутылка")

    result = await core.list_add(db_pool, session_id, "пиво", amount=1, unit="бутылка")

    assert result["status"] == "updated"
    assert result["previous_quantity"] == "2 бут."
    assert result["quantity"] == "1 бут."
    assert "2 бут." in result["say"] and "1 бут." in result["say"]
    assert "пиво" in result["say"]
    # No rendered block: it is relayed verbatim, and answering "я поменял"
    # with eleven unchanged rows hides the one line that changed.
    assert "rendered" not in result


async def test_a_first_amount_still_answers_with_the_list(db_pool):
    """Nothing was overwritten, so there is no old value to name and the
    list is the answer as for any other addition."""
    session_id = await _new_session(db_pool)
    await core.list_add(db_pool, session_id, "пиво")

    result = await core.list_add(db_pool, session_id, "пиво", amount=2, unit="бутылка")

    assert result["status"] == "updated"
    assert "rendered" in result
    assert "say" not in result


def test_the_refusal_never_hands_the_model_a_menu_of_numbers():
    """Live: "скажи, сколько всего должно быть: пять штук, шесть штук?" — two
    numbers invented out of nothing, in place of the one answer the person
    actually has.

    The model was not being creative. Both the system instruction and this
    tool's ask_user ended with a list of example amounts, and it copied the
    shape rather than the point.

    So the examples are gone rather than negated. A prompt cannot show the
    wrong output and expect it not to be copied — which is why this asserts
    the bad shape is absent from the text sent to the model, not merely that
    a rule against it was added. The explanation lives in a code comment,
    where the model never sees it.
    """
    import bot.router as router

    instruction = " ".join(router._ACTIVE_MODE_SYSTEM_INSTRUCTION.split())
    assert "две бутылки, три бутылки" not in instruction
    assert "пять штук" not in instruction, "not even as something to avoid"
    assert "never offer numbers" in instruction, \
        "forbidding it is the fix; deleting the example alone lets it come back"

    ask_user = " ".join(inspect.getsource(core.list_add).split())
    assert "never offer numbers" in ask_user
    assert "пять штук" not in ask_user
