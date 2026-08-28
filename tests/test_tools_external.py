from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from google.genai import errors

import bot.tools.external as external


class _Proxy:
    async def _request(self, model, prompt, **kwargs):
        response = await external.gemini.aio.models.generate_content(
            model=model, contents=prompt, config=None
        )
        return {"result": response.text or "", "sources": external._extract_sources(response)}


class _Gemini:
    class aio:
        class models:
            generate_content = AsyncMock(return_value=None)


@pytest.fixture(autouse=True)
def proxy_test_backend(monkeypatch):
    monkeypatch.setattr(external, "gemini", _Gemini, raising=False)
    monkeypatch.setattr(external, "proxy", _Proxy())


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


import httpx


def _http_response(json_body):
    resp = MagicMock()
    resp.json.return_value = json_body
    return resp


async def test_maps_lookup_found(monkeypatch):
    body = {
        "places": [{
            "displayName": {"text": "Hanania Meadow"},
            "formattedAddress": "Route 1, North District",
            "location": {"latitude": 32.79, "longitude": 35.05},
            "rating": 4.2,
            "reviews": [{"text": {"text": "Great spot, no official grilling though."}}],
        }]
    }
    monkeypatch.setattr(httpx.AsyncClient, "post", AsyncMock(return_value=_http_response(body)))

    result = await external.maps_lookup("Hanania meadow")

    assert result["found"] is True
    assert result["name"] == "Hanania Meadow"
    assert result["lat"] == 32.79
    assert result["review_snippets"] == ["Great spot, no official grilling though."]


async def test_maps_lookup_not_found(monkeypatch):
    monkeypatch.setattr(httpx.AsyncClient, "post", AsyncMock(return_value=_http_response({"places": []})))

    result = await external.maps_lookup("a place nobody has heard of")

    assert result == {"found": False}


async def test_weather_lookup_found(monkeypatch):
    body = {"daily": {
        "time": ["2026-09-01"],
        "weathercode": [3],
        "temperature_2m_max": [29.5],
        "temperature_2m_min": [21.0],
        "precipitation_probability_max": [10],
    }}
    monkeypatch.setattr(httpx.AsyncClient, "get", AsyncMock(return_value=_http_response(body)))

    result = await external.weather_lookup(32.79, 35.05, "2026-09-01")

    assert result["found"] is True
    assert result["temp_max_c"] == 29.5
    assert result["precipitation_probability_max"] == 10


async def test_weather_lookup_uses_archive_for_past_dates(monkeypatch):
    body = {"daily": {
        "time": ["2025-07-04"],
        "weather_code": [1],
        "temperature_2m_max": [31.0],
        "temperature_2m_min": [23.0],
        "precipitation_sum": [0.0],
    }}
    get = AsyncMock(return_value=_http_response(body))
    monkeypatch.setattr(httpx.AsyncClient, "get", get)

    result = await external.weather_lookup(31.959019, 34.927831, "2025-07-04")

    assert result == {
        "found": True,
        "weather_code": 1,
        "temp_max_c": 31.0,
        "temp_min_c": 23.0,
        "precipitation_sum_mm": 0.0,
    }
    assert get.await_args.args[0] == "https://archive-api.open-meteo.com/v1/archive"


async def test_weather_lookup_date_out_of_range(monkeypatch):
    monkeypatch.setattr(
        httpx.AsyncClient, "get",
        AsyncMock(return_value=_http_response({"daily": {"time": []}})),
    )

    result = await external.weather_lookup(32.79, 35.05, "2030-01-01")

    assert result == {"found": False}


def test_build_external_registry_covers_every_external_tool():
    registry = external.build_external_registry()

    assert set(registry) == {"web_search", "maps_lookup", "weather_lookup"}


# --- Extra hardening tests: honesty-on-failure (R7/R10) beyond the brief's 8. ---


def _http_error_response():
    request = MagicMock()
    response = MagicMock()
    response.status_code = 500
    resp = MagicMock()
    resp.raise_for_status.side_effect = httpx.HTTPStatusError(
        "server error", request=request, response=response
    )
    return resp


def _malformed_json_response():
    resp = MagicMock()
    resp.raise_for_status.return_value = None
    resp.json.side_effect = ValueError("not json")
    return resp


async def test_maps_lookup_not_found_on_non_200(monkeypatch):
    monkeypatch.setattr(httpx.AsyncClient, "post", AsyncMock(return_value=_http_error_response()))

    result = await external.maps_lookup("anything")

    assert result == {"found": False}


async def test_maps_lookup_not_found_on_malformed_json(monkeypatch):
    monkeypatch.setattr(httpx.AsyncClient, "post", AsyncMock(return_value=_malformed_json_response()))

    result = await external.maps_lookup("anything")

    assert result == {"found": False}


async def test_maps_lookup_not_found_on_connect_error(monkeypatch):
    monkeypatch.setattr(
        httpx.AsyncClient, "post",
        AsyncMock(side_effect=httpx.ConnectError("connection refused")),
    )

    result = await external.maps_lookup("anything")

    assert result == {"found": False}


async def test_maps_lookup_not_found_on_read_timeout(monkeypatch):
    monkeypatch.setattr(
        httpx.AsyncClient, "post",
        AsyncMock(side_effect=httpx.ReadTimeout("timed out")),
    )

    result = await external.maps_lookup("anything")

    assert result == {"found": False}


async def test_maps_lookup_not_found_when_location_missing(monkeypatch):
    body = {
        "places": [{
            "displayName": {"text": "Somewhere"},
            "formattedAddress": "Nowhere St",
        }]
    }
    monkeypatch.setattr(httpx.AsyncClient, "post", AsyncMock(return_value=_http_response(body)))

    result = await external.maps_lookup("somewhere")

    assert result == {"found": False}


async def test_weather_lookup_not_found_on_non_200(monkeypatch):
    monkeypatch.setattr(httpx.AsyncClient, "get", AsyncMock(return_value=_http_error_response()))

    result = await external.weather_lookup(32.79, 35.05, "2026-09-01")

    assert result == {"found": False}


async def test_weather_lookup_not_found_on_malformed_json(monkeypatch):
    monkeypatch.setattr(httpx.AsyncClient, "get", AsyncMock(return_value=_malformed_json_response()))

    result = await external.weather_lookup(32.79, 35.05, "2026-09-01")

    assert result == {"found": False}


async def test_weather_lookup_not_found_on_connect_error(monkeypatch):
    monkeypatch.setattr(
        httpx.AsyncClient, "get",
        AsyncMock(side_effect=httpx.ConnectError("connection refused")),
    )

    result = await external.weather_lookup(32.79, 35.05, "2026-09-01")

    assert result == {"found": False}


async def test_weather_lookup_not_found_on_read_timeout(monkeypatch):
    monkeypatch.setattr(
        httpx.AsyncClient, "get",
        AsyncMock(side_effect=httpx.ReadTimeout("timed out")),
    )

    result = await external.weather_lookup(32.79, 35.05, "2026-09-01")

    assert result == {"found": False}


async def test_weather_lookup_not_found_when_daily_arrays_short(monkeypatch):
    # "time" lists the date but the parallel arrays are missing entries —
    # must not raise IndexError.
    body = {"daily": {
        "time": ["2026-09-01"],
        "weathercode": [],
        "temperature_2m_max": [29.5],
        "temperature_2m_min": [21.0],
        "precipitation_probability_max": [10],
    }}
    monkeypatch.setattr(httpx.AsyncClient, "get", AsyncMock(return_value=_http_response(body)))

    result = await external.weather_lookup(32.79, 35.05, "2026-09-01")

    assert result == {"found": False}


# --- regression tests for code-review findings ---

async def test_web_search_falls_back_to_the_next_model_on_429(monkeypatch):
    """The API key's quota is shared process-wide. Pinning MODELS[0] meant that
    once the primary model was rate-limited, every search reported 'couldn't
    find anything' while the rest of the bot degraded gracefully."""
    from google.genai import errors

    calls = []

    async def flaky(*args, model=None, **kwargs):
        calls.append(model)
        if len(calls) == 1:
            raise errors.APIError(code=429, response_json={"error": {"code": 429}}, response=None)
        return _resp("Grilling is allowed.", ["https://example.com/x"])

    monkeypatch.setattr(external.gemini.aio.models, "generate_content", flaky)

    result = await external.web_search("can we grill there")

    assert result["found"] is True
    assert calls == external.SEARCH_MODELS[:2]


async def test_web_search_excludes_models_without_search_support():
    """Gemma has no Google Search tool, so it must not appear in the chain."""
    assert external.SEARCH_MODELS
    assert all(m.startswith("gemini-") for m in external.SEARCH_MODELS)


async def test_web_search_stops_on_a_non_retryable_error(monkeypatch):
    from google.genai import errors

    calls = []

    async def bad_request(*args, model=None, **kwargs):
        calls.append(model)
        raise errors.APIError(code=400, response_json={"error": {"code": 400}}, response=None)

    monkeypatch.setattr(external.gemini.aio.models, "generate_content", bad_request)

    assert await external.web_search("x") == {"found": False, "summary": None, "sources": []}
    assert len(calls) == 1  # did not burn the rest of the chain


async def test_web_search_fails_closed_on_transport_error(monkeypatch):
    """httpx errors are not APIError subclasses; the documented contract is
    that any failure yields found=False."""
    async def boom(*args, **kwargs):
        raise httpx.ConnectError("dns failure")

    monkeypatch.setattr(external.gemini.aio.models, "generate_content", boom)

    assert await external.web_search("x") == {"found": False, "summary": None, "sources": []}


async def test_maps_lookup_rejects_a_result_with_no_usable_name(monkeypatch):
    """places.name is TEXT NOT NULL and is the cache key S6 matches on, so a
    nameless result must be not-found rather than a row that fails to insert."""
    body = {"places": [{"location": {"latitude": 1.0, "longitude": 2.0}}]}
    monkeypatch.setattr(httpx.AsyncClient, "post", AsyncMock(return_value=_http_response(body)))

    assert await external.maps_lookup("nameless") == {"found": False}


async def test_maps_lookup_falls_back_to_address_when_display_name_missing(monkeypatch):
    body = {"places": [{
        "formattedAddress": "Route 1, North District",
        "location": {"latitude": 1.0, "longitude": 2.0},
    }]}
    monkeypatch.setattr(httpx.AsyncClient, "post", AsyncMock(return_value=_http_response(body)))

    result = await external.maps_lookup("x")

    assert result["found"] is True
    assert result["name"] == "Route 1, North District"


async def test_maps_lookup_drops_reviews_with_no_text(monkeypatch):
    body = {"places": [{
        "displayName": {"text": "Somewhere"},
        "location": {"latitude": 1.0, "longitude": 2.0},
        "reviews": [{"text": {"text": "Nice"}}, {"rating": 4}, {"text": {}}],
    }]}
    monkeypatch.setattr(httpx.AsyncClient, "post", AsyncMock(return_value=_http_response(body)))

    result = await external.maps_lookup("x")

    assert result["review_snippets"] == ["Nice"]


async def test_weather_lookup_not_found_when_the_row_is_all_nulls(monkeypatch):
    """Open-Meteo returns nulls past the forecast horizon; reporting
    temp_max_c=None as a found forecast is the confident-but-empty answer R10
    forbids."""
    body = {"daily": {
        "time": ["2026-09-01"], "weathercode": [None],
        "temperature_2m_max": [None], "temperature_2m_min": [None],
        "precipitation_probability_max": [None],
    }}
    monkeypatch.setattr(httpx.AsyncClient, "get", AsyncMock(return_value=_http_response(body)))

    assert await external.weather_lookup(1.0, 2.0, "2026-09-01") == {"found": False}


async def test_http_tools_share_one_client():
    """The epic specifies a single shared AsyncClient rather than one per call."""
    first = external._client()
    second = external._client()

    assert first is second
    await external.aclose()


@pytest.mark.skip(reason="Google SDK AFC is no longer used; search is delegated to ai-proxy")
async def test_web_search_does_not_use_automatic_function_calling():
    """Google Search executes server-side, so there is nothing for the SDK to
    call back into. Without saying so explicitly, generate_content takes its
    AFC path and logs a warning recommending a chat session — which this call
    has no use for."""
    from google.genai import _extra_utils

    captured = {}

    async def fake_generate_content(*, model, contents, config):
        captured["config"] = config
        raise RuntimeError("stop here — only the config matters")

    with patch("bot.tools.external.gemini") as client:
        client.aio.models.generate_content = fake_generate_content
        await external.web_search("что угодно")

    assert _extra_utils.should_disable_afc(captured["config"]) is True
