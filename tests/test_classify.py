import json
from unittest.mock import AsyncMock

import pytest
from google.genai import errors

from bot.ai import classify as classify_module


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
    from google.genai import types

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
