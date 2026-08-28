import logging
import os
import time
from datetime import date as date_type

import httpx
from google.genai import types

from bot.ai.client import MODELS
from bot.ai.proxy import proxy
from bot.logging_setup import truncate

log = logging.getLogger(__name__)

_HTTP_TIMEOUT = 10.0

# Grounded search needs a model with a real search tool behind it. Groq's
# GPT-OSS models carry a server-side `browser_search`; Gemini has its own
# Google Search grounding. Gemma has neither, so it is excluded even though it
# is the last resort in the chat chain.
SEARCH_MODELS = MODELS

# Seconds. Without this a hung grounded-search call blocks the tool loop, and
# with it the user's Telegram reply, indefinitely.
_SEARCH_TIMEOUT_MS = 30_000

_shared_client: httpx.AsyncClient | None = None


def _client() -> httpx.AsyncClient:
    """One shared AsyncClient for all outbound HTTP, per the epic's constraint.

    Created lazily so importing this module never needs a running event loop,
    and reused so a maps+weather sequence inside a single tool loop doesn't pay
    for a fresh TLS handshake each time.
    """
    global _shared_client
    if _shared_client is None or _shared_client.is_closed:
        _shared_client = httpx.AsyncClient(timeout=_HTTP_TIMEOUT)
    return _shared_client


async def aclose() -> None:
    """Close the shared client. Called on process shutdown."""
    global _shared_client
    if _shared_client is not None and not _shared_client.is_closed:
        await _shared_client.aclose()
    _shared_client = None


def _extract_sources(resp) -> list[str]:
    sources = []
    for candidate in getattr(resp, "candidates", None) or []:
        gm = getattr(candidate, "grounding_metadata", None)
        if not gm:
            continue
        for chunk in gm.grounding_chunks or []:
            uri = getattr(getattr(chunk, "web", None), "uri", None)
            if uri:
                sources.append(uri)
    return sources


async def web_search(query: str) -> dict:
    """Grounded search via Gemini's built-in Google Search tool. Never invents
    an answer: an empty result or a total failure both come back as
    found=False rather than falling through to the model's own (possibly
    stale) knowledge.

    Walks SEARCH_MODELS rather than pinning the primary model: the API key's
    quota is shared process-wide, so once the primary starts returning 429
    every search would otherwise report "couldn't find anything" while the
    rest of the bot happily degrades to a later model.
    """
    if proxy is None:
        return {"found": False, "summary": None, "sources": []}
    try:
        data = await proxy._request(
            os.environ.get("AI_PROXY_MODEL", MODELS[0]), query,
            tools=[{"type": "browser_search"}],
        )
    except Exception:
        log.warning("web_search via ai-proxy failed for query=%r", query, exc_info=True)
        return {"found": False, "summary": None, "sources": []}
    text = (data.get("result") or "").strip()
    return {"found": bool(text), "summary": text or None, "sources": []}


_MAPS_API_KEY = os.environ["GOOGLE_MAPS_API_KEY"]
_TEXT_SEARCH_URL = "https://places.googleapis.com/v1/places:searchText"
_WEATHER_URL = "https://api.open-meteo.com/v1/forecast"
_WEATHER_ARCHIVE_URL = "https://archive-api.open-meteo.com/v1/archive"


async def maps_lookup(query: str) -> dict:
    """Text-search a place via Google Places. Any failure mode — non-200,
    a transport error, unparsable JSON, or a response missing the fields
    we need — comes back as found=False rather than raising, so a flaky
    upstream never crashes the tool loop or produces a half-built answer."""
    started = time.perf_counter()
    # The API key travels in a header and is deliberately never logged.
    log.debug("maps_lookup -> %s | query=%s", _TEXT_SEARCH_URL, truncate(query, 150))
    try:
        resp = await _client().post(
            _TEXT_SEARCH_URL,
            json={"textQuery": query},
            headers={
                "X-Goog-Api-Key": _MAPS_API_KEY,
                "X-Goog-FieldMask": (
                    "places.displayName,places.formattedAddress,"
                    "places.location,places.rating,places.reviews"
                ),
            },
        )
        resp.raise_for_status()
        body = resp.json()
    except httpx.HTTPError:
        log.warning("maps_lookup request failed for query=%r", query, exc_info=True)
        return {"found": False}
    except ValueError:
        log.warning("maps_lookup returned malformed JSON for query=%r", query, exc_info=True)
        return {"found": False}

    if not isinstance(body, dict):
        return {"found": False}

    places = body.get("places") or []
    if not places or not isinstance(places[0], dict):
        return {"found": False}

    p = places[0]
    location = p.get("location") or {}
    lat = location.get("latitude")
    lon = location.get("longitude")
    if lat is None or lon is None:
        return {"found": False}

    # places.name is TEXT NOT NULL and is also the cache key S6 matches on, so
    # a result with no usable name is worse than no result at all — it would
    # fail the insert rather than degrade.
    name = (p.get("displayName") or {}).get("text") or p.get("formattedAddress")
    if not name:
        return {"found": False}

    log.debug("maps_lookup <- %.0fms | name=%s | lat=%s lon=%s | rating=%s",
              (time.perf_counter() - started) * 1000, name, lat, lon, p.get("rating"))
    return {
        "found": True,
        "name": name,
        "address": p.get("formattedAddress"),
        "lat": lat,
        "lon": lon,
        "rating": p.get("rating"),
        "review_snippets": [
            (r.get("text") or {}).get("text")
            for r in (p.get("reviews") or [])[:5]
            if isinstance(r, dict) and (r.get("text") or {}).get("text")
        ],
    }


async def weather_lookup(lat: float, lon: float, date: str) -> dict:
    """Daily forecast via Open-Meteo. Same fail-safe contract as
    maps_lookup: non-200, transport errors, malformed JSON, or a response
    missing the requested date's fields all resolve to found=False."""
    started = time.perf_counter()
    log.debug("weather_lookup -> lat=%s lon=%s date=%s", lat, lon, date)
    try:
        requested_date = date_type.fromisoformat(date)
    except (TypeError, ValueError):
        return {"found": False}

    is_past = requested_date < date_type.today()
    url = _WEATHER_ARCHIVE_URL if is_past else _WEATHER_URL
    daily = (
        "weather_code,temperature_2m_max,temperature_2m_min,precipitation_sum"
        if is_past else
        "weathercode,temperature_2m_max,temperature_2m_min,precipitation_probability_max"
    )
    try:
        resp = await _client().get(url, params={
            "latitude": lat, "longitude": lon,
            "daily": daily,
            "timezone": "auto", "start_date": date, "end_date": date,
        })
        resp.raise_for_status()
        body = resp.json()
    except httpx.HTTPError:
        log.warning("weather_lookup request failed for lat=%r lon=%r date=%r", lat, lon, date, exc_info=True)
        return {"found": False}
    except ValueError:
        log.warning("weather_lookup returned malformed JSON for lat=%r lon=%r date=%r", lat, lon, date, exc_info=True)
        return {"found": False}

    if not isinstance(body, dict):
        return {"found": False}

    daily = body.get("daily") or {}
    dates = daily.get("time") or []
    if date not in dates:
        return {"found": False}

    idx = dates.index(date)
    try:
        weather_code_key = "weather_code" if is_past else "weathercode"
        forecast = {
            "found": True,
            "weather_code": daily[weather_code_key][idx],
            "temp_max_c": daily["temperature_2m_max"][idx],
            "temp_min_c": daily["temperature_2m_min"][idx],
        }
        if is_past:
            forecast["precipitation_sum_mm"] = daily["precipitation_sum"][idx]
        else:
            forecast["precipitation_probability_max"] = daily["precipitation_probability_max"][idx]
        # Open-Meteo returns nulls for variables it can't supply (e.g. a date
        # past the forecast horizon). Reporting temp_max_c=None as a "found"
        # forecast would be exactly the confident-but-empty answer R10 forbids.
        if forecast["temp_max_c"] is None and forecast["weather_code"] is None:
            log.debug("weather_lookup <- %.0fms | all-null row, treating as not found",
                      (time.perf_counter() - started) * 1000)
            return {"found": False}
        log.debug("weather_lookup <- %.0fms | %s",
                  (time.perf_counter() - started) * 1000, truncate(forecast))
        return forecast
    except (KeyError, IndexError, TypeError):
        log.warning("weather_lookup response missing expected daily fields for date=%r", date, exc_info=True)
        return {"found": False}


def build_external_registry() -> dict:
    return {
        "web_search": web_search,
        "maps_lookup": maps_lookup,
        "weather_lookup": weather_lookup,
    }
