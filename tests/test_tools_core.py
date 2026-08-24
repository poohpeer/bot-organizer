import bot.tools.core as core


async def _new_session(db_pool, chat_id=1):
    await db_pool.execute("INSERT INTO chats (chat_id, title) VALUES ($1, 'Chat')", chat_id)
    row = await db_pool.fetchrow(
        "INSERT INTO sessions (chat_id, activity_type) VALUES ($1, 'picnic') RETURNING id",
        chat_id,
    )
    return row["id"]


async def test_remember_and_get_fact(db_pool):
    session_id = await _new_session(db_pool)

    await core.remember_fact(db_pool, session_id, "destination", "Hanania meadow")
    facts = await core.get_facts(db_pool, session_id, key="destination")

    assert facts["facts"]["destination"] == "Hanania meadow"


async def test_get_facts_returns_latest_value_when_updated(db_pool):
    session_id = await _new_session(db_pool)

    await core.remember_fact(db_pool, session_id, "destination", "First place")
    await core.remember_fact(db_pool, session_id, "destination", "Second place")
    facts = await core.get_facts(db_pool, session_id, key="destination")

    assert facts["facts"]["destination"] == "Second place"


async def test_get_facts_without_key_returns_all_latest(db_pool):
    session_id = await _new_session(db_pool)

    await core.remember_fact(db_pool, session_id, "destination", "Hanania meadow")
    await core.remember_fact(db_pool, session_id, "headcount", "12")
    facts = await core.get_facts(db_pool, session_id)

    assert facts["facts"] == {"destination": "Hanania meadow", "headcount": "12"}


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
        db_pool, session_id, chat_id=1, message="Bring the grill",
        remind_at="2026-09-01T09:00:00",
    )

    assert result["status"] == "ok"
    row = await db_pool.fetchrow("SELECT * FROM reminders WHERE id = $1", result["reminder_id"])
    assert row["status"] == "pending"
    assert row["message"] == "Bring the grill"


async def test_reminder_cancel_marks_cancelled(db_pool):
    session_id = await _new_session(db_pool)
    created = await core.reminder_set(
        db_pool, session_id, chat_id=1, message="x", remind_at="2026-09-01T09:00:00"
    )

    result = await core.reminder_cancel(db_pool, created["reminder_id"])

    assert result["status"] == "ok"
    row = await db_pool.fetchrow("SELECT status FROM reminders WHERE id = $1", created["reminder_id"])
    assert row["status"] == "cancelled"


async def test_reminder_cancel_missing_id(db_pool):
    result = await core.reminder_cancel(db_pool, 999999)

    assert result["status"] == "not_found"


async def test_broadcast_message_is_gated(db_pool):
    session_id = await _new_session(db_pool)

    result = await core.broadcast_message(db_pool, session_id, chat_id=1, text="Heads up everyone")

    assert result["status"] == "pending_confirmation"


def test_build_core_registry_covers_every_core_tool(db_pool):
    from unittest.mock import AsyncMock

    registry = core.build_core_registry(db_pool, AsyncMock())

    assert set(registry) == {
        "remember_fact", "get_facts", "list_add", "list_show", "list_check_off",
        "list_remove_item", "set_participant", "get_participants",
        "nudge_unconfirmed_participants", "reminder_set", "reminder_cancel",
        "broadcast_message",
    }
