"""Conversation history, so a question the bot asks can be answered."""

import bot.decision_log as decision_log
import bot.history as history


async def _log(pool, chat_id, text, reply, stage="tool_call"):
    await decision_log.log_decision(
        pool, chat_id=chat_id, user_id=1, raw_text=text, stage=stage,
        decision={"session_id": 1, "reply": reply} if reply else {"session_id": 1},
    )


async def test_the_exchange_comes_back_oldest_first(db_pool):
    """Live: the bot asked for the whole new amount, the person replied
    "1 литр", and the bot answered "Что именно 1 литр?" — it had no idea it
    had just asked."""
    await _log(db_pool, 1, "Добавь ещё 0.5 воды", "Вода уже есть как 0.5 бут. Сколько всего?")
    await _log(db_pool, 1, "1 литр", "Записал.")

    turns = await history.recent_turns(db_pool, 1)

    assert turns == [
        {"role": "user", "content": "Добавь ещё 0.5 воды"},
        {"role": "assistant", "content": "Вода уже есть как 0.5 бут. Сколько всего?"},
        {"role": "user", "content": "1 литр"},
        {"role": "assistant", "content": "Записал."},
    ]


async def test_another_chat_is_not_visible(db_pool):
    """The obvious one, and the one that would leak a group's plans."""
    await _log(db_pool, 1, "наш вопрос", "наш ответ")
    await _log(db_pool, 2, "чужой вопрос", "чужой ответ")

    turns = await history.recent_turns(db_pool, 1)

    assert all("чужой" not in turn["content"] for turn in turns)


async def test_the_silent_capture_path_is_left_out(db_pool):
    """Those turns never replied. Including them would put the group's own
    chatter in front of the model as if the bot had been part of it."""
    await _log(db_pool, 1, "просто болтаем", None, stage="silent_capture")
    await _log(db_pool, 1, "@bot покажи список", "Вот список.")

    turns = await history.recent_turns(db_pool, 1)

    assert [t["content"] for t in turns] == ["@bot покажи список", "Вот список."]


async def test_a_turn_the_bot_stayed_silent_on_contributes_no_reply(db_pool):
    """Inventing one would tell the model it said something it did not."""
    await _log(db_pool, 1, "@bot ок", None)

    turns = await history.recent_turns(db_pool, 1)

    assert turns == [{"role": "user", "content": "@bot ок"}]


async def test_only_the_last_few_turns_come_back(db_pool):
    for i in range(history.CONVERSATION_TURNS + 4):
        await _log(db_pool, 1, f"вопрос {i}", f"ответ {i}")

    turns = await history.recent_turns(db_pool, 1)

    assert len(turns) == history.CONVERSATION_TURNS * 2
    assert turns[-1]["content"] == f"ответ {history.CONVERSATION_TURNS + 3}"


async def test_a_stale_exchange_is_not_offered_as_context(db_pool):
    """"1 литр" answering something nobody remembers asking is worse than no
    context: it makes the bot act on a thread the group has moved on from."""
    await _log(db_pool, 1, "старый вопрос", "старый ответ")
    await db_pool.execute(
        "UPDATE decision_log SET created_at = now() - $1 * interval '1 minute' WHERE chat_id = 1",
        history.CONVERSATION_MAX_AGE_MINUTES + 1,
    )

    assert await history.recent_turns(db_pool, 1) == []
