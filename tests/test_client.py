import groq
import httpx
import pytest
from google.genai import errors as gemini_errors

from bot.ai.client import AllModelsUnavailable, ModelFallback
from bot.ai.providers import Reply, is_retryable


def _groq_error(status):
    request = httpx.Request("POST", "https://api.groq.com/openai/v1/chat/completions")
    response = httpx.Response(status, request=request, json={"error": {"message": "x"}})
    cls = {429: groq.RateLimitError, 400: groq.BadRequestError}.get(
        status, groq.InternalServerError
    )
    return cls("boom", response=response, body=None)


def _gemini_error(code):
    return gemini_errors.APIError(code, {"error": {"message": "x", "code": code}})


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


def test_retryable_covers_both_sdks_and_nothing_else():
    """The chain exists for shared-quota and overload failures. A 400 means the
    request is wrong; repeating it elsewhere just spends another call."""
    assert is_retryable(_groq_error(429)) is True
    assert is_retryable(_groq_error(503)) is True
    assert is_retryable(_gemini_error(429)) is True
    assert is_retryable(_gemini_error(500)) is True
    assert is_retryable(_groq_error(400)) is False
    assert is_retryable(_gemini_error(400)) is False
    assert is_retryable(ValueError("not an API error")) is False


def test_a_gemini_error_with_no_usable_code_is_not_retryable():
    """`code` is None when the body carries no numeric code, and the SDK does
    not guarantee it is an int. Comparing either to 500 would raise TypeError
    from inside an exception handler and bury the real failure."""
    assert is_retryable(gemini_errors.APIError(None, {})) is False
    assert is_retryable(gemini_errors.APIError("Service Unavailable", {})) is False


async def test_the_first_groq_model_is_tried_first():
    """The whole point of the reordering: Groq ahead of Gemini."""
    groq_provider, gemini_provider = _FakeProvider("groq"), _FakeProvider("gemini")
    chain = [(groq_provider, "openai/gpt-oss-120b"), (gemini_provider, "gemini-3.6-flash")]

    provider, model, _chat = await ModelFallback(chain).start("привет")

    assert (provider.name, model) == ("groq", "openai/gpt-oss-120b")
    assert gemini_provider.started == []


async def test_a_rate_limited_groq_model_falls_through_to_the_second():
    groq_provider = _FakeProvider("groq", {"openai/gpt-oss-120b": _groq_error(429)})
    chain = [(groq_provider, "openai/gpt-oss-120b"), (groq_provider, "openai/gpt-oss-20b")]

    _provider, model, _chat = await ModelFallback(chain).start("привет")

    assert model == "openai/gpt-oss-20b"


async def test_both_groq_models_failing_falls_through_to_gemini():
    """The case the user asked for explicitly: two Groq models, then Google."""
    groq_provider = _FakeProvider("groq", {
        "openai/gpt-oss-120b": _groq_error(429),
        "openai/gpt-oss-20b": _groq_error(503),
    })
    gemini_provider = _FakeProvider("gemini")
    chain = [
        (groq_provider, "openai/gpt-oss-120b"),
        (groq_provider, "openai/gpt-oss-20b"),
        (gemini_provider, "gemini-3.6-flash"),
    ]

    provider, model, _chat = await ModelFallback(chain).start("привет")

    assert (provider.name, model) == ("gemini", "gemini-3.6-flash")
    assert groq_provider.started == ["openai/gpt-oss-120b", "openai/gpt-oss-20b"]


async def test_every_model_failing_raises_all_models_unavailable():
    """Distinct from a plain API error so the router can tell "the AI is
    unreachable" — which has its own reply — from a bug."""
    provider = _FakeProvider("groq", {"a": _groq_error(429), "b": _groq_error(500)})

    with pytest.raises(AllModelsUnavailable):
        await ModelFallback([(provider, "a"), (provider, "b")]).start("привет")


async def test_a_non_retryable_error_is_raised_immediately():
    provider = _FakeProvider("groq", {"a": _groq_error(400)})
    chain = [(provider, "a"), (provider, "b")]

    with pytest.raises(groq.BadRequestError):
        await ModelFallback(chain).start("привет")

    assert provider.started == ["a"], "must not have tried the next model"


async def test_the_switch_is_sticky():
    """Quotas are shared process-wide: having just learned a model is rate
    limited, trying it again on the next message spends another call to be
    told the same thing."""
    provider = _FakeProvider("groq", {"a": _groq_error(429)})
    chain = [(provider, "a"), (provider, "b")]
    chosen = ModelFallback(chain)

    await chosen.start("первое сообщение")
    provider.started.clear()
    await chosen.start("второе сообщение")

    assert provider.started == ["b"], "should not have re-tried the rate-limited model"


def test_the_chain_puts_the_two_gpt_oss_models_last():
    from bot.ai.client import GEMINI_MODELS, build_chain

    chain = build_chain(_FakeProvider("groq"), _FakeProvider("gemini"))

    assert [(p.name, m) for p, m in chain[-2:]] == [
        ("groq", "openai/gpt-oss-120b"),
        ("groq", "openai/gpt-oss-20b"),
    ]
    assert [m for p, m in chain[:-2]] == GEMINI_MODELS


def test_without_a_groq_key_the_chain_is_gemini_only():
    """Groq sits in front of Gemini to add resilience; it is not what the bot
    is built on. A missing key must degrade, not stop the bot."""
    from bot.ai.client import GEMINI_MODELS, build_chain

    chain = build_chain(None, _FakeProvider("gemini"))

    assert [m for p, m in chain] == GEMINI_MODELS


def test_a_provider_failing_to_parse_its_own_model_is_retryable():
    """Groq answers 400 output_parse_failed when it cannot parse the tool call
    its own model emitted — observed on gpt-oss-20b in 3 of 6 tool-calling
    runs. It is a 400, but it says nothing about our request, and treating it
    as fatal answers half those turns with "не понял, переформулируй"."""
    request = httpx.Request("POST", "https://api.groq.com/x")
    body = {"error": {"message": "Parsing failed.", "type": "invalid_request_error",
                      "code": "output_parse_failed"}}
    response = httpx.Response(400, request=request, json=body)
    error = groq.BadRequestError("parse failed", response=response, body=body)

    assert is_retryable(error) is True


def test_an_ordinary_bad_request_is_still_fatal():
    """Only the parse failure is special. A genuinely malformed request would
    fail identically on every model."""
    request = httpx.Request("POST", "https://api.groq.com/x")
    body = {"error": {"message": "bad", "type": "invalid_request_error", "code": "invalid_value"}}
    response = httpx.Response(400, request=request, json=body)

    assert is_retryable(groq.BadRequestError("bad", response=response, body=body)) is False
