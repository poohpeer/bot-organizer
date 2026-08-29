"""The two guards on what a turn is allowed to say, on the MCP path.

bot/ai/tool_loop.py sees every tool result and enforces both. A CLI provider
runs its tools over MCP instead and never passes through that loop, so until
the grant carried a record of the turn the CLI path had neither guard.

The live failure this exists for: the bot asked "сколько бутылок должно быть
всего", the person answered "Две бутылки пива", codex called list_add — the
row was updated to 2 бут. — and answered "<silent>". Nothing reached the
chat, so from the group's side the bot had gone dead.
"""

from unittest.mock import AsyncMock

import bot.mcp_server as mcp_server
import bot.router as router
from bot.turn_outcome import ACKNOWLEDGEMENT, TurnRecord

from tests.test_router import (
    BOT_ID,
    BOT_USERNAME,
    _message,
    _new_active_session,
)

LIST = "Ещё не разобрали:\n◻️ пиво, 2 бут."


async def _turn(db_pool, monkeypatch, *, reply, does):
    """One addressed turn where the CLI did `does` and answered `reply`."""
    grants = mcp_server.GrantStore()
    monkeypatch.setattr(router, "MCP_BASE_URL", "http://bot:8081")
    router.set_grant_store(grants)
    monkeypatch.setattr(router, "classify", AsyncMock(return_value=False))
    active = await _new_active_session(db_pool)
    telegram_bot = AsyncMock()

    async def fake_run_tool_loop(model_fn, text, registry, *, system_instruction,
                                 history=None, mcp_url=None):
        grant = grants.resolve(mcp_url.rsplit("/", 1)[-1])
        does(grant.record)
        return reply

    monkeypatch.setattr(router, "run_tool_loop", fake_run_tool_loop)
    try:
        await router.handle_active_message(
            db_pool, telegram_bot, active, _message("Две бутылки пива"),
            BOT_ID, BOT_USERNAME,
        )
    finally:
        router.set_grant_store(None)
    return telegram_bot


async def test_silence_after_a_change_sends_what_the_list_now_says(db_pool, monkeypatch):
    telegram_bot = await _turn(
        db_pool, monkeypatch,
        reply="<silent>",
        does=lambda record: record.record("list_add", {"status": "added", "rendered": LIST}),
    )

    telegram_bot.send_message.assert_awaited_once()
    assert telegram_bot.send_message.await_args.kwargs["text"] == LIST


async def test_silence_after_a_change_with_nothing_rendered_still_answers(db_pool, monkeypatch):
    """remember_fact returns no block. "Записал." is a poor sentence and an
    infinitely better outcome than nothing at all."""
    telegram_bot = await _turn(
        db_pool, monkeypatch,
        reply="<silent>",
        does=lambda record: record.record("remember_fact", {"status": "ok"}),
    )

    telegram_bot.send_message.assert_awaited_once()
    assert telegram_bot.send_message.await_args.kwargs["text"] == ACKNOWLEDGEMENT


async def test_a_turn_that_only_read_may_still_be_silent(db_pool, monkeypatch):
    """The sentinel is not being taken away — a message that only gives the
    bot something to look at and needs no reply still gets none."""
    telegram_bot = await _turn(
        db_pool, monkeypatch,
        reply="<silent>",
        does=lambda record: record.record("get_facts", {"facts": []}),
    )

    telegram_bot.send_message.assert_not_awaited()


async def test_answering_privately_stays_silent_in_the_group(db_pool, monkeypatch):
    """send_private_message already delivered a message, and it went to a DM
    precisely because the person did not want the group answered."""
    telegram_bot = await _turn(
        db_pool, monkeypatch,
        reply="<silent>",
        does=lambda record: record.record("send_private_message", {"status": "sent"}),
    )

    telegram_bot.send_message.assert_not_awaited()


async def test_a_turn_with_no_tools_at_all_stays_silent(db_pool, monkeypatch):
    telegram_bot = await _turn(db_pool, monkeypatch, reply="<silent>", does=lambda record: None)

    telegram_bot.send_message.assert_not_awaited()


async def test_a_rendered_block_the_cli_dropped_is_sent_anyway(db_pool, monkeypatch):
    """The same rule the tool loop applies to its own turns: a renderer's
    output is the answer, and a model that talks around it gets overruled."""
    telegram_bot = await _turn(
        db_pool, monkeypatch,
        reply="Больше нет элементов в списке.",
        does=lambda record: record.record("list_show", {"rendered": LIST}),
    )

    assert telegram_bot.send_message.await_args.kwargs["text"] == LIST


async def test_a_reply_that_already_carries_the_block_is_left_alone(db_pool, monkeypatch):
    said = f"Вот что осталось.\n{LIST}"
    telegram_bot = await _turn(
        db_pool, monkeypatch,
        reply=said,
        does=lambda record: record.record("list_show", {"rendered": LIST}),
    )

    assert telegram_bot.send_message.await_args.kwargs["text"] == said


async def test_a_refusal_survives_the_block_that_came_with_it(db_pool, monkeypatch):
    """list_add's already_present result carries both the list and an ask_user.
    Relaying the list would throw away the only part that answers the person."""
    refusal = "Пиво уже есть: 1 бут. Скажи, сколько должно быть всего."
    telegram_bot = await _turn(
        db_pool, monkeypatch,
        reply=refusal,
        does=lambda record: record.record(
            "list_add",
            {"status": "already_present", "ask_user": "say the total", "rendered": LIST},
        ),
    )

    assert telegram_bot.send_message.await_args.kwargs["text"] == refusal


def test_a_tool_that_raised_is_not_recorded_as_a_change():
    """mcp_server records after the call returns, so a failure cannot make
    the bot announce something that never happened."""
    record = TurnRecord()

    assert record.changed_something() is False
    record.record("list_show", {"rendered": LIST})
    assert record.changed_something() is False
    record.record("list_add", {"status": "added"})
    assert record.changed_something() is True
