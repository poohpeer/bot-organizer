from unittest.mock import AsyncMock

import pytest

from bot.ai.client import AllModelsUnavailable, ModelFallback
from bot.ai.providers import Reply, ToolCall
from bot.ai.proxy import ProxyError
from bot.ai.tool_loop import MAX_TOOL_ITERATIONS, run_tool_loop


def _rate_limited():
    """What a rate limit actually looks like here.

    This used to build a groq.RateLimitError, a type no longer reachable in
    this process: every model call goes through ai-proxy and surfaces as
    ProxyError. The recovery below was therefore green against an error
    production could never raise, while the real one fell straight through
    tool_loop's `if not is_retryable(e): raise`.
    """
    return ProxyError(429, "ai-proxy returned 429")


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


async def test_a_dropped_rendered_block_is_sent_instead_of_the_models_own_words():
    """The live failure. Asked "покажи список" the model called list_show,
    received the rendered list, called event_status, received the full report
    — then answered "Больше нет элементов в списке. Что ещё нужно?", throwing
    both away while the data sat in front of it.
    """
    report = "Вот текущая информация:\n\n🛒 Список:\n◻️ пиво"
    chat = _ScriptedChat([
        Reply(text="", tool_calls=[ToolCall(name="event_status", args={}, id="c1")]),
        Reply(text="Больше нет элементов в списке. Что ещё нужно?"),
    ])
    provider = _ScriptedProvider("ai-proxy", [chat])

    answer = await run_tool_loop(
        _fallback((provider, "openai/gpt-oss-120b")),
        "покажи список",
        {"event_status": AsyncMock(return_value={"status": "ok", "report": report})},
    )

    assert answer == report


async def test_a_reply_that_already_carries_the_block_is_left_alone():
    """Compliance plus additions is compliance. Overwriting a reply that
    included the block and added the roster-gap remark would throw away real
    information to satisfy a rule about formatting."""
    rendered = "◻️ Витька\n✅ Андрюха"
    composed = f"{rendered}\n\nВ чате 9 человек, но записаны только 2."
    chat = _ScriptedChat([
        Reply(text="", tool_calls=[ToolCall(name="get_participants", args={}, id="c1")]),
        Reply(text=composed),
    ])
    provider = _ScriptedProvider("ai-proxy", [chat])

    answer = await run_tool_loop(
        _fallback((provider, "openai/gpt-oss-120b")),
        "кто идёт?",
        {"get_participants": AsyncMock(return_value={"participants": [], "rendered": rendered})},
    )

    assert answer == composed


async def test_a_tool_with_no_rendered_block_leaves_the_reply_untouched():
    """Most tools return data, not prose. Their results must not be mistaken
    for something to relay verbatim."""
    chat = _ScriptedChat([
        Reply(text="", tool_calls=[ToolCall(name="remember_fact", args={}, id="c1")]),
        Reply(text="Записал."),
    ])
    provider = _ScriptedProvider("ai-proxy", [chat])

    answer = await run_tool_loop(
        _fallback((provider, "openai/gpt-oss-120b")),
        "место — море",
        {"remember_fact": AsyncMock(return_value={"status": "ok", "key": "place"})},
    )

    assert answer == "Записал."


async def test_running_out_of_turns_still_sends_what_was_gathered():
    """Seen live: after adding three items the model called event_status four
    times in a row, exhausted the six-turn limit, and the person got the
    generic failure message — while a correct, freshly rendered report had
    come back from every one of those calls."""
    report = "Вот текущая информация:\n\n🛒 Список:\n◻️ мясо"
    chat = _ScriptedChat([
        Reply(text="", tool_calls=[ToolCall(name="event_status", args={}, id=f"c{i}")])
        for i in range(MAX_TOOL_ITERATIONS + 2)
    ])
    provider = _ScriptedProvider("ai-proxy", [chat])

    answer = await run_tool_loop(
        _fallback((provider, "openai/gpt-oss-120b")),
        "покажи статус",
        {"event_status": AsyncMock(return_value={"status": "ok", "report": report})},
    )

    assert answer == report


async def test_running_out_of_turns_with_nothing_gathered_still_raises():
    """No block means nothing useful to send; the router's generic reply is
    the honest outcome."""
    chat = _ScriptedChat([
        Reply(text="", tool_calls=[ToolCall(name="remember_fact", args={}, id=f"c{i}")])
        for i in range(MAX_TOOL_ITERATIONS + 2)
    ])
    provider = _ScriptedProvider("ai-proxy", [chat])

    with pytest.raises(RuntimeError):
        await run_tool_loop(
            _fallback((provider, "openai/gpt-oss-120b")),
            "запомни",
            {"remember_fact": AsyncMock(return_value={"status": "ok"})},
        )


async def test_repeating_the_same_call_stops_instead_of_burning_the_limit():
    """Live: after adding three items the model called event_status four times
    in a row with identical arguments, took the turn limit and ~15s to do it,
    and every call returned the same report."""
    report = "Вот текущая информация:\n\n🛒 Список:\n◻️ мясо"
    chat = _ScriptedChat([
        Reply(text="", tool_calls=[ToolCall(name="event_status", args={"session_id": 1}, id=f"c{i}")])
        for i in range(MAX_TOOL_ITERATIONS + 2)
    ])
    provider = _ScriptedProvider("ai-proxy", [chat])
    tool = AsyncMock(return_value={"status": "ok", "report": report})

    answer = await run_tool_loop(
        _fallback((provider, "openai/gpt-oss-120b")), "покажи статус", {"event_status": tool},
    )

    assert answer == report
    # Once, not six times: the repeat is recognised before it is executed, so
    # the identical call never runs at all.
    assert tool.await_count == 1


async def test_a_different_call_is_not_treated_as_a_repeat():
    """Only an identical request stops the loop; ordinary multi-step work must
    still run to completion."""
    chat = _ScriptedChat([
        Reply(text="", tool_calls=[ToolCall(name="list_add", args={"name": "мясо"}, id="c1")]),
        Reply(text="", tool_calls=[ToolCall(name="list_add", args={"name": "пиво"}, id="c2")]),
        Reply(text="Добавил."),
    ])
    provider = _ScriptedProvider("ai-proxy", [chat])
    tool = AsyncMock(return_value={"status": "ok", "rendered": "◻️ мясо"})

    answer = await run_tool_loop(
        _fallback((provider, "openai/gpt-oss-120b")), "добавь мясо и пиво", {"list_add": tool},
    )

    assert tool.await_count == 2
    assert answer == "◻️ мясо" or answer == "Добавил."
