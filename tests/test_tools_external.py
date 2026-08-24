from unittest.mock import AsyncMock, MagicMock

from google.genai import errors

import bot.tools.external as external


def _resp(text, sources=()):
    resp = MagicMock()
    resp.text = text
    chunk_mocks = []
    for uri in sources:
        chunk = MagicMock()
        chunk.web.uri = uri
        chunk_mocks.append(chunk)
    candidate = MagicMock()
    candidate.grounding_metadata.grounding_chunks = chunk_mocks
    resp.candidates = [candidate]
    return resp


async def test_web_search_found_returns_summary_and_sources(monkeypatch):
    monkeypatch.setattr(
        external.gemini.aio.models, "generate_content",
        AsyncMock(return_value=_resp("Grilling is allowed on weekends.", ["https://example.com/rules"])),
    )

    result = await external.web_search("can we grill at Hanania meadow")

    assert result["found"] is True
    assert result["summary"] == "Grilling is allowed on weekends."
    assert result["sources"] == ["https://example.com/rules"]


async def test_web_search_not_found_when_empty_text(monkeypatch):
    monkeypatch.setattr(
        external.gemini.aio.models, "generate_content",
        AsyncMock(return_value=_resp("")),
    )

    result = await external.web_search("something obscure nobody wrote about")

    assert result["found"] is False
    assert result["summary"] is None
    assert result["sources"] == []


async def test_web_search_not_found_on_api_error(monkeypatch):
    async def boom(*args, **kwargs):
        raise errors.APIError(code=500, response_json={}, response=None)

    monkeypatch.setattr(external.gemini.aio.models, "generate_content", boom)

    result = await external.web_search("anything")

    assert result["found"] is False
