"""The model-fallback chain.

Every case here is written against ProxyError, because that is now the only
failure a model call in this bot can produce: there is no Groq or Gemini SDK
client anywhere in it, so those SDKs' exception types can never reach this
code. The previous version of this file tested exactly those unreachable
types, which is how a real gap stayed hidden — see
test_a_proxy_error_is_what_the_chain_actually_sees.
"""

import pytest

from bot.ai.client import AllModelsUnavailable, ModelFallback
from bot.ai.providers import Reply
from bot.ai.proxy import ProxyError, is_retryable


def _proxy_error(status):
    return ProxyError(status, f"ai-proxy returned {status}")


class _FakeProvider:
    """A provider that fails a set number of times before answering."""

    def __init__(self, name, failures=()):
        self.name = name
        self.failures = dict(failures)
        self.started = []

    async def start(self, model, prompt, **kwargs):
        self.started.append(model)
        if model in self.failures:
            raise self.failures[model]
        chat = type("Chat", (), {"reply": Reply(text=f"answered by {model}")})()
        return chat


def test_retryable_covers_overload_and_nothing_else():
    """The chain exists for shared-quota and overload failures. A 400 means the
    request is wrong; repeating it elsewhere just spends another call."""
    assert is_retryable(_proxy_error(429)) is True
    assert is_retryable(_proxy_error(500)) is True
    assert is_retryable(_proxy_error(502)) is True
    assert is_retryable(_proxy_error(503)) is True
    assert is_retryable(_proxy_error(400)) is False
    assert is_retryable(_proxy_error(404)) is False
    assert is_retryable(ValueError("not a proxy error")) is False


def test_a_proxy_error_is_what_the_chain_actually_sees():
    """The gap this file used to hide. is_retryable lived in providers.py and
    understood only Groq/Gemini SDK exceptions, so it answered False for the
    one error type that can actually occur. client.py had grown an inline
    ProxyError branch and kept working; tool_loop.py had not, so a 429 partway
    through a tool conversation raised instead of re-driving on the next
    model. Both call sites now share this one function.
    """
    import bot.ai.client as client
    import bot.ai.tool_loop as tool_loop

    assert client.is_retryable is is_retryable
    assert tool_loop.is_retryable is is_retryable
    assert is_retryable(_proxy_error(429)) is True


async def test_the_first_model_is_tried_first():
    provider = _FakeProvider("ai-proxy")
    chain = [(provider, "openai/gpt-oss-120b"), (provider, "gemini-3.6-flash")]

    used, model, _chat = await ModelFallback(chain).start("привет")

    assert (used.name, model) == ("ai-proxy", "openai/gpt-oss-120b")
    assert provider.started == ["openai/gpt-oss-120b"]


async def test_a_rate_limited_model_falls_through_to_the_second():
    provider = _FakeProvider("ai-proxy", {"openai/gpt-oss-120b": _proxy_error(429)})
    chain = [(provider, "openai/gpt-oss-120b"), (provider, "openai/gpt-oss-20b")]

    _used, model, _chat = await ModelFallback(chain).start("привет")

    assert model == "openai/gpt-oss-20b"


async def test_both_gpt_oss_models_failing_falls_through_to_gemini():
    """Groq ahead of Gemini is the whole point of the ordering: a separate
    account and quota, so one provider's rate limit does not stop the bot."""
    provider = _FakeProvider("ai-proxy", {
        "openai/gpt-oss-120b": _proxy_error(429),
        "openai/gpt-oss-20b": _proxy_error(503),
    })
    chain = [
        (provider, "openai/gpt-oss-120b"),
        (provider, "openai/gpt-oss-20b"),
        (provider, "gemini-3.6-flash"),
    ]

    _used, model, _chat = await ModelFallback(chain).start("привет")

    assert model == "gemini-3.6-flash"
    assert provider.started == [
        "openai/gpt-oss-120b", "openai/gpt-oss-20b", "gemini-3.6-flash",
    ]


async def test_every_model_failing_raises_all_models_unavailable():
    """Distinct from a plain API error so the router can tell "the AI is
    unreachable" — which has its own reply — from a bug."""
    provider = _FakeProvider("ai-proxy", {"a": _proxy_error(429), "b": _proxy_error(500)})

    with pytest.raises(AllModelsUnavailable):
        await ModelFallback([(provider, "a"), (provider, "b")]).start("привет")


async def test_a_non_retryable_error_is_raised_immediately():
    provider = _FakeProvider("ai-proxy", {"a": _proxy_error(400)})
    chain = [(provider, "a"), (provider, "b")]

    with pytest.raises(ProxyError):
        await ModelFallback(chain).start("привет")

    assert provider.started == ["a"], "must not have tried the next model"


async def test_the_switch_is_sticky():
    """Quotas are shared process-wide: having just learned a model is rate
    limited, trying it again on the next message spends another call to be
    told the same thing."""
    provider = _FakeProvider("ai-proxy", {"a": _proxy_error(429)})
    chain = [(provider, "a"), (provider, "b")]
    chosen = ModelFallback(chain)

    await chosen.start("первое сообщение")
    provider.started.clear()
    await chosen.start("второе сообщение")

    assert provider.started == ["b"], "should not have re-tried the rate-limited model"


def test_the_chain_runs_http_providers_first_and_the_clis_as_reserve():
    """Order is the whole design. Groq first — a separate account from
    Gemini, so one provider's rate limit does not stop the bot. The CLIs
    last, because they reach tools over MCP and cost far more per turn:
    measured on one task, codex spent 5 546 uncached input tokens against
    Groq's 152. They are here for the evenings Groq spends returning 429."""
    from bot.ai.client import DEFAULT_PROXY_MODELS

    models = DEFAULT_PROXY_MODELS.split(",")

    assert models[:2] == ["openai/gpt-oss-120b", "openai/gpt-oss-20b"]
    assert all(m.startswith(("gemma-", "gemini-")) for m in models[2:-2])
    assert models[-2:] == ["codex:", "claude:sonnet"], "the CLIs are the reserve, not the front"


def test_codex_comes_before_claude():
    """Not a token-count decision: codex is on a free account, and tokens
    that cost nothing outrank a token count."""
    from bot.ai.client import DEFAULT_PROXY_MODELS

    models = DEFAULT_PROXY_MODELS.split(",")

    assert models.index("codex:") < models.index("claude:sonnet")


def test_the_bot_holds_no_model_credentials_or_endpoints():
    """The point of routing everything through ai-proxy. A key or provider URL
    reappearing here is a direct path back to a model provider, and a wider
    blast radius for this bot's Secret than it needs.
    """
    import pathlib

    root = pathlib.Path(__file__).resolve().parent.parent
    banned = ("GROQ_API_KEY", "GEMINI_API_KEY", "api.groq.com", "generativelanguage")
    offenders = []
    for path in list((root / "bot").rglob("*.py")) + list((root / "worker").rglob("*.py")):
        text = path.read_text(encoding="utf-8")
        for needle in banned:
            if needle in text:
                offenders.append(f"{path.relative_to(root)}: {needle}")
    assert offenders == [], f"model credentials/endpoints leaked back in: {offenders}"


def test_no_provider_sdk_is_imported_for_calling_models():
    """google.genai stays as a *schema* library (bot/tools/schema.py writes
    tool declarations in its types, converted to the OpenAI dialect by
    bot/ai/tool_schema_openai.py). What must not come back is a client:
    genai.Client or groq.AsyncGroq means this process can call a provider
    directly again.
    """
    import pathlib

    root = pathlib.Path(__file__).resolve().parent.parent
    offenders = []
    for path in list((root / "bot").rglob("*.py")) + list((root / "worker").rglob("*.py")):
        text = path.read_text(encoding="utf-8")
        for needle in ("genai.Client(", "AsyncGroq(", "import groq"):
            if needle in text:
                offenders.append(f"{path.relative_to(root)}: {needle}")
    assert offenders == [], f"a provider SDK client came back: {offenders}"


def test_the_default_chain_names_only_models_ai_proxy_accepts():
    """Half of this chain used to be dead. ai-proxy's /v1/models advertised
    gemma-4-26b-a4b-it, gemini-3.5-flash-lite and gemini-3.1-flash-lite, but
    its gemini adapter rejected all three with 400 model_not_found — and a 400
    is not retryable, so reaching one aborted the chain instead of advancing
    past it. Verified live against the running proxy: the four names below all
    answer 200.
    """
    from bot.ai.client import DEFAULT_PROXY_MODELS

    dead = {"gemma-4-26b-a4b-it", "gemini-3.5-flash-lite", "gemini-3.1-flash-lite"}
    models = set(DEFAULT_PROXY_MODELS.split(","))

    assert models & dead == set(), f"chain names models ai-proxy rejects: {models & dead}"
    assert {"gemini-3.7-flash", "gemini-3.6-flash", "gemini-3.5-flash", "gemma-4-31b-it"} <= models
