from unittest.mock import AsyncMock

import groq
import httpx
import pytest

from bot.ai.client import AllModelsUnavailable, ModelFallback
from bot.ai.providers import Reply, ToolCall
from bot.ai.tool_loop import MAX_TOOL_ITERATIONS, run_tool_loop


def _rate_limited():
    request = httpx.Request("POST", "https://api.groq.com/x")
    response = httpx.Response(429, request=request, json={"error": {"message": "x"}})
    return groq.RateLimitError("rate limited", response=response, body=None)


class _ScriptedChat:
    """Replays a list of Replies, one per turn."""

    def __init__(self, replies, fail_on_turn=None):
        self.replies = list(replies)
        self.fail_on_turn = fail_on_turn
        self.turn = 0
        self.reply = self.replies.pop(0)
        self.results_seen = []

    async def send_tool_results(self, results):
        self.turn += 1
        self.results_seen.append(results)
        if self.fail_on_turn == self.turn:
            raise _rate_limited()
        self.reply = self.replies.pop(0)
        return self.reply

    def history(self):
        return [{"role": "user", "content": "..."}]


class _ScriptedProvider:
    def __init__(self, name, chats):
        self.name = name
        self.chats = list(chats)
        self.calls = []

    async def start(self, model, prompt, **kwargs):
        self.calls.append((model, prompt, kwargs.get("system_instruction")))
        return self.chats.pop(0)


def _fallback(*entries):
    return ModelFallback(list(entries))


async def test_plain_answer_returns_immediately():
    provider = _ScriptedProvider("groq", [_ScriptedChat([Reply(text="Готово.")])])

    result = await run_tool_loop(_fallback((provider, "m")), "привет", {})

    assert result == "Готово."


async def test_a_requested_tool_is_run_and_its_result_fed_back():
    chat = _ScriptedChat([
        Reply(text="", tool_calls=[ToolCall(name="list_show", args={"session_id": 1}, id="c1")]),
        Reply(text="Помидоры, огурцы."),
    ])
    provider = _ScriptedProvider("groq", [chat])
    registry = {"list_show": AsyncMock(return_value={"items": ["помидоры"]})}

    result = await run_tool_loop(_fallback((provider, "m")), "что в списке?", registry)

    assert result == "Помидоры, огурцы."
    registry["list_show"].assert_awaited_once_with(session_id=1)
    assert chat.results_seen[0][0][1] == {"items": ["помидоры"]}


async def test_a_non_dict_tool_result_is_wrapped():
    """Both providers want an object for a tool result; several tools return
    lists (list_show, get_participants, archive_lookup)."""
    chat = _ScriptedChat([
        Reply(text="", tool_calls=[ToolCall(name="t", args={}, id="c1")]),
        Reply(text="ок"),
    ])
    provider = _ScriptedProvider("groq", [chat])

    await run_tool_loop(_fallback((provider, "m")), "?", {"t": AsyncMock(return_value=["a", "b"])})

    assert chat.results_seen[0][0][1] == {"result": ["a", "b"]}


async def test_an_unknown_tool_is_reported_to_the_model_not_raised():
    chat = _ScriptedChat([
        Reply(text="", tool_calls=[ToolCall(name="nope", args={}, id="c1")]),
        Reply(text="понял"),
    ])
    provider = _ScriptedProvider("groq", [chat])

    assert await run_tool_loop(_fallback((provider, "m")), "?", {}) == "понял"
    assert "unknown tool" in chat.results_seen[0][0][1]["error"]


async def test_a_raising_tool_becomes_a_result_not_a_crash():
    chat = _ScriptedChat([
        Reply(text="", tool_calls=[ToolCall(name="t", args={}, id="c1")]),
        Reply(text="ладно"),
    ])
    provider = _ScriptedProvider("groq", [chat])

    result = await run_tool_loop(
        _fallback((provider, "m")), "?", {"t": AsyncMock(side_effect=RuntimeError("нет базы"))}
    )

    assert result == "ладно"
    assert chat.results_seen[0][0][1] == {"error": "нет базы"}


async def test_the_system_instruction_reaches_the_provider():
    provider = _ScriptedProvider("groq", [_ScriptedChat([Reply(text="ок")])])

    await run_tool_loop(_fallback((provider, "m")), "?", {}, system_instruction="будь краток")

    assert provider.calls[0][2] == "будь краток"


async def test_a_rate_limit_on_a_later_turn_re_drives_on_the_next_model():
    """Only the opening turn goes through the chain. A 429 mid-conversation is
    if anything more likely — quota runs out partway — and losing the whole
    exchange to it would be worse than continuing elsewhere."""
    failing = _ScriptedChat(
        [Reply(text="", tool_calls=[ToolCall(name="t", args={}, id="c1")])], fail_on_turn=1
    )
    groq_provider = _ScriptedProvider("groq", [failing])
    gemini_provider = _ScriptedProvider("gemini", [_ScriptedChat([Reply(text="досказал")])])

    result = await run_tool_loop(
        _fallback((groq_provider, "gpt-oss-120b"), (gemini_provider, "gemini-3.6-flash")),
        "?", {"t": AsyncMock(return_value={})},
    )

    assert result == "досказал"
    assert gemini_provider.calls[0][1] is None, "re-drive carries history, not a new prompt"


async def test_a_rate_limit_with_no_model_left_propagates():
    failing = _ScriptedChat(
        [Reply(text="", tool_calls=[ToolCall(name="t", args={}, id="c1")])], fail_on_turn=1
    )
    provider = _ScriptedProvider("groq", [failing])

    with pytest.raises(AllModelsUnavailable):
        await run_tool_loop(_fallback((provider, "only")), "?", {"t": AsyncMock(return_value={})})


async def test_an_endless_tool_caller_raises_rather_than_looping():
    calling = Reply(text="", tool_calls=[ToolCall(name="t", args={}, id="c1")])
    chat = _ScriptedChat([calling] * (MAX_TOOL_ITERATIONS + 2))
    provider = _ScriptedProvider("groq", [chat])

    with pytest.raises(RuntimeError, match="exceeded max iterations"):
        await run_tool_loop(_fallback((provider, "m")), "?", {"t": AsyncMock(return_value={})})


async def test_text_on_the_final_allowed_turn_is_still_returned():
    """A conversation that legitimately uses every round must still get its
    answer back rather than being thrown away at the limit."""
    calling = Reply(text="", tool_calls=[ToolCall(name="t", args={}, id="c1")])
    chat = _ScriptedChat([calling] * MAX_TOOL_ITERATIONS + [Reply(text="успел")])
    provider = _ScriptedProvider("groq", [chat])

    result = await run_tool_loop(_fallback((provider, "m")), "?", {"t": AsyncMock(return_value={})})

    assert result == "успел"
