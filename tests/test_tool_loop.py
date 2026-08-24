from unittest.mock import AsyncMock, MagicMock

import pytest

from bot.ai.tool_loop import run_tool_loop


def _make_call(name, args):
    call = MagicMock()
    call.name = name
    call.args = args
    return call


async def test_run_tool_loop_executes_function_call_and_returns_text():
    chat = MagicMock()
    first_resp = MagicMock(function_calls=[_make_call("list_add", {"session_id": 1, "name": "tomatoes"})])
    second_resp = MagicMock(function_calls=None, text="Added tomatoes.")
    chat.send_message = AsyncMock(return_value=second_resp)

    fb = MagicMock()
    fb.send_message = AsyncMock(return_value=("gemini-3.6-flash", chat, first_resp))

    list_add = AsyncMock(return_value={"status": "ok"})
    registry = {"list_add": list_add}

    result = await run_tool_loop(fb, "add tomatoes to the list", registry)

    assert result == "Added tomatoes."
    list_add.assert_awaited_once_with(session_id=1, name="tomatoes")


async def test_run_tool_loop_returns_text_directly_with_no_tool_calls():
    chat = MagicMock()
    resp = MagicMock(function_calls=None, text="Hello.")
    fb = MagicMock()
    fb.send_message = AsyncMock(return_value=("gemini-3.6-flash", chat, resp))

    result = await run_tool_loop(fb, "hi", {})

    assert result == "Hello."


async def test_run_tool_loop_reports_unknown_tool_without_crashing():
    chat = MagicMock()
    first_resp = MagicMock(function_calls=[_make_call("no_such_tool", {})])
    second_resp = MagicMock(function_calls=None, text="ok")
    chat.send_message = AsyncMock(return_value=second_resp)
    fb = MagicMock()
    fb.send_message = AsyncMock(return_value=("gemini-3.6-flash", chat, first_resp))

    result = await run_tool_loop(fb, "do something weird", {})

    assert result == "ok"
    sent_parts = chat.send_message.await_args.args[0]
    assert "unknown tool" in sent_parts[0].function_response.response["error"]


async def test_run_tool_loop_passes_system_instruction_through():
    chat = MagicMock()
    resp = MagicMock(function_calls=None, text="ok")
    fb = MagicMock()
    fb.send_message = AsyncMock(return_value=("gemini-3.6-flash", chat, resp))

    await run_tool_loop(fb, "hi", {}, system_instruction="Stay quiet unless asked.")

    sent_config = fb.send_message.await_args.kwargs["config"]
    assert sent_config.system_instruction == "Stay quiet unless asked."


async def test_run_tool_loop_raises_after_max_iterations():
    chat = MagicMock()
    looping_resp = MagicMock(function_calls=[_make_call("list_add", {"session_id": 1, "name": "x"})])
    chat.send_message = AsyncMock(return_value=looping_resp)
    fb = MagicMock()
    fb.send_message = AsyncMock(return_value=("gemini-3.6-flash", chat, looping_resp))
    registry = {"list_add": AsyncMock(return_value={"status": "ok"})}

    with pytest.raises(RuntimeError, match="max iterations"):
        await run_tool_loop(fb, "loop forever", registry)
