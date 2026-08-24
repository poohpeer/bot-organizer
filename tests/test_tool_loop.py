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


async def test_run_tool_loop_wraps_non_dict_tool_result():
    """Part.from_function_response requires a dict; tools that return a list
    (list_show, get_participants, archive_lookup) must not crash the loop."""
    chat = MagicMock()
    first_resp = MagicMock(function_calls=[_make_call("list_show", {"session_id": 1})])
    second_resp = MagicMock(function_calls=None, text="Here it is.")
    chat.send_message = AsyncMock(return_value=second_resp)
    fb = MagicMock()
    fb.send_message = AsyncMock(return_value=("gemini-3.6-flash", chat, first_resp))
    registry = {"list_show": AsyncMock(return_value=["tomatoes", "meat"])}

    result = await run_tool_loop(fb, "show the list", registry)

    assert result == "Here it is."
    sent = chat.send_message.await_args.args[0]
    assert sent[0].function_response.response == {"result": ["tomatoes", "meat"]}


async def test_run_tool_loop_returns_text_on_final_allowed_iteration():
    """A conversation using exactly MAX_TOOL_ITERATIONS tool rounds and then
    answering must return that answer, not raise."""
    from bot.ai.tool_loop import MAX_TOOL_ITERATIONS

    call_resp = MagicMock(function_calls=[_make_call("list_add", {"session_id": 1, "name": "x"})])
    final_resp = MagicMock(function_calls=None, text="Done at the limit.")
    chat = MagicMock()
    chat.send_message = AsyncMock(
        side_effect=[call_resp] * (MAX_TOOL_ITERATIONS - 1) + [final_resp]
    )
    fb = MagicMock()
    fb.send_message = AsyncMock(return_value=("gemini-3.6-flash", chat, call_resp))
    registry = {"list_add": AsyncMock(return_value={"status": "ok"})}

    assert await run_tool_loop(fb, "work hard", registry) == "Done at the limit."


async def test_run_tool_loop_falls_back_to_next_model_on_later_turn_429():
    """A 429 on turn 2+ must re-drive on the next model, not abort."""
    from google.genai import errors

    chat1 = MagicMock()
    chat1.send_message = AsyncMock(
        side_effect=errors.APIError(code=429, response_json={"error": {"code": 429}}, response=None)
    )
    chat1.get_history = MagicMock(return_value=["prior"])
    chat2 = MagicMock()

    first_resp = MagicMock(function_calls=[_make_call("list_add", {"session_id": 1, "name": "x"})])
    recovered = MagicMock(function_calls=None, text="Recovered.")

    fb = MagicMock()
    fb.index = 0
    fb.models = ["a", "b"]
    fb.send_message = AsyncMock(
        side_effect=[("a", chat1, first_resp), ("b", chat2, recovered)]
    )
    registry = {"list_add": AsyncMock(return_value={"status": "ok"})}

    result = await run_tool_loop(fb, "add x", registry)

    assert result == "Recovered."
    assert fb.index == 1


async def test_run_tool_loop_reraises_when_no_model_left_to_fall_back_to():
    from google.genai import errors

    chat = MagicMock()
    chat.send_message = AsyncMock(
        side_effect=errors.APIError(code=429, response_json={"error": {"code": 429}}, response=None)
    )
    fb = MagicMock()
    fb.index = 1
    fb.models = ["a", "b"]
    fb.send_message = AsyncMock(
        return_value=("b", chat, MagicMock(function_calls=[_make_call("list_add", {"name": "x"})]))
    )

    with pytest.raises(errors.APIError):
        await run_tool_loop(fb, "add x", {"list_add": AsyncMock(return_value={})})
