import json
from unittest.mock import AsyncMock, patch

from google.genai import errors

from google.genai import types

from bot.ai import classify as classify_module

_SCHEMA = types.Schema(
    type=types.Type.OBJECT, properties={"result": types.Schema(type=types.Type.BOOLEAN)}
)


async def test_classify_parses_true(monkeypatch):
    resp = AsyncMock()
    resp.text = json.dumps({"result": True})
    generate = AsyncMock(return_value=resp)
    monkeypatch.setattr(classify_module.gemini.aio.models, "generate_content", generate)

    result = await classify_module.classify("Is this about food?", "let's get pizza")

    assert result is True
    generate.assert_awaited_once()


async def test_classify_parses_false(monkeypatch):
    resp = AsyncMock()
    resp.text = json.dumps({"result": False})
    monkeypatch.setattr(
        classify_module.gemini.aio.models, "generate_content", AsyncMock(return_value=resp)
    )

    assert await classify_module.classify("Is this about food?", "nice weather today") is False


async def test_classify_fails_closed_on_api_error(monkeypatch):
    async def boom(*args, **kwargs):
        raise errors.APIError(code=500, response_json={}, response=None)

    monkeypatch.setattr(classify_module.gemini.aio.models, "generate_content", boom)

    assert await classify_module.classify("Is this about food?", "anything") is False


async def test_extract_returns_parsed_json(monkeypatch):
    
    resp = AsyncMock()
    resp.text = json.dumps({"is_start": True, "activity_type": "picnic"})
    monkeypatch.setattr(classify_module.gemini.aio.models, "generate_content", AsyncMock(return_value=resp))
    schema = types.Schema(
        type=types.Type.OBJECT,
        properties={"is_start": types.Schema(type=types.Type.BOOLEAN), "activity_type": types.Schema(type=types.Type.STRING)},
        required=["is_start"],
    )

    result = await classify_module.extract("...", "let's track the picnic", schema)

    assert result == {"is_start": True, "activity_type": "picnic"}


async def test_extract_fails_closed_to_empty_dict(monkeypatch):
    from google.genai import types

    async def boom(*args, **kwargs):
        raise errors.APIError(code=500, response_json={}, response=None)

    monkeypatch.setattr(classify_module.gemini.aio.models, "generate_content", boom)
    schema = types.Schema(type=types.Type.OBJECT, properties={}, required=[])

    assert await classify_module.extract("...", "anything", schema) == {}


async def test_extract_fails_closed_when_text_is_none(monkeypatch):
    """resp.text is None for a safety-blocked or text-less candidate;
    json.loads(None) raises TypeError, which must not escape."""
    resp = AsyncMock()
    resp.text = None
    monkeypatch.setattr(
        classify_module.gemini.aio.models, "generate_content", AsyncMock(return_value=resp)
    )

    assert await classify_module.classify("Is this about food?", "anything") is False


async def test_extract_fails_closed_on_non_object_json(monkeypatch):
    """The model may ignore response_schema and return an array; .get() on it
    would raise AttributeError instead of failing closed."""
    resp = AsyncMock()
    resp.text = json.dumps([1, 2, 3])
    monkeypatch.setattr(
        classify_module.gemini.aio.models, "generate_content", AsyncMock(return_value=resp)
    )

    assert await classify_module.classify("Is this about food?", "anything") is False


async def test_extract_fails_closed_on_transport_error(monkeypatch):
    """httpx errors are not APIError subclasses — a network blip must still
    fail closed, not propagate into the caller."""
    import httpx

    async def boom(*args, **kwargs):
        raise httpx.ConnectError("connection refused")

    monkeypatch.setattr(classify_module.gemini.aio.models, "generate_content", boom)

    assert await classify_module.classify("Is this about food?", "anything") is False


async def test_extract_does_not_use_automatic_function_calling():
    """generate_content takes the SDK's AFC path unless explicitly told not to,
    even with no tools in the config, and logs a warning about it. This call
    runs on every incoming message, so the warning is not merely cosmetic
    noise — it is the hottest path in the bot."""
    from google.genai import _extra_utils

    captured = {}

    async def fake_generate_content(*, model, contents, config):
        captured["config"] = config
        raise RuntimeError("stop here — only the config matters")

    with patch("bot.ai.classify.gemini") as client:
        client.aio.models.generate_content = fake_generate_content
        await classify_module.extract("инструкция", "текст", _SCHEMA)

    assert _extra_utils.should_disable_afc(captured["config"]) is True
