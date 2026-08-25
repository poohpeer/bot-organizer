import logging
import os
import time

import httpx
from google.genai import errors, types

from bot.ai.client import GROQ_MODELS, MODELS, gemini, groq_client
from bot.ai.providers import is_retryable
from bot.logging_setup import truncate

log = logging.getLogger(__name__)

_HTTP_TIMEOUT = 10.0

# Grounded search needs a model with a real search tool behind it. Groq's
# GPT-OSS models carry a server-side `browser_search`; Gemini has its own
# Google Search grounding. Gemma has neither, so it is excluded even though it
# is the last resort in the chat chain.
GROQ_SEARCH_MODELS = GROQ_MODELS if groq_client is not None else []
SEARCH_MODELS = [m for m in MODELS if m.startswith("gemini-")]

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
    last_error = None

    for model in GROQ_SEARCH_MODELS:
        started = time.perf_counter()
        log.debug("web_search -> groq/%s | query=%s", model, truncate(query, 150))
        try:
            resp = await groq_client.chat.completions.create(
                model=model,
                messages=[{"role": "user", "content": query}],
                tools=[{"type": "browser_search"}],
                tool_choice="required",
            )
        except Exception as e:
            last_error = e
            if not is_retryable(e):
                log.warning("web_search on groq/%s failed non-retryably", model, exc_info=True)
                break
            log.warning("web_search on groq/%s unavailable (%s), trying the next", model, e)
            continue

        text = (resp.choices[0].message.content or "").strip()
        elapsed = (time.perf_counter() - started) * 1000
        if text:
            log.debug("web_search <- groq/%s %.0fms | %s", model, elapsed, truncate(text))
            # Groq's browser_search does not surface the pages it read, so
            # there are no source URLs to hand back. Saying so honestly beats
            # inventing them.
            return {"found": True, "summary": text, "sources": []}
        log.debug("web_search <- groq/%s %.0fms | empty result", model, elapsed)

    for model in SEARCH_MODELS:
        started = time.perf_counter()
        log.debug("web_search -> gemini/%s | query=%s", model, truncate(query, 150))
        try:
            resp = await gemini.aio.models.generate_content(
                model=model,
                contents=query,
                config=types.GenerateContentConfig(
                    tools=[types.Tool(google_search=types.GoogleSearch())],
                    automatic_function_calling=types.AutomaticFunctionCallingConfig(disable=True),
                    http_options=types.HttpOptions(timeout=_SEARCH_TIMEOUT_MS),
                ),
            )
        except errors.APIError as e:
            last_error = e
            retryable = e.code == 429 or (e.code is not None and e.code >= 500)
            if retryable:
                log.warning("web_search on gemini/%s failed (%s), trying next model", model, e.code)
                continue
            log.warning("web_search on gemini/%s failed non-retryably (%s)", model, e.code)
            break
        except Exception as e:
            # Transport failures (httpx ConnectError/ReadTimeout, DNS) are not
            # APIError subclasses; the contract says any failure is found=False,
            # so they must not escape either.
            last_error = e
            log.warning("web_search on gemini/%s raised %s", model, type(e).__name__, exc_info=True)
            break

        text = (resp.text or "").strip()
        elapsed = (time.perf_counter() - started) * 1000
        if not text:
            log.debug("web_search <- gemini/%s %.0fms | empty result", model, elapsed)
            return {"found": False, "summary": None, "sources": []}
        sources = _extract_sources(resp)
        log.debug("web_search <- gemini/%s %.0fms | %d sources | %s", model, elapsed, len(sources), truncate(text))
        return {"found": True, "summary": text, "sources": sources}

    if last_error is not None:
        log.warning("web_search exhausted every search model for query=%r", query)
    return {"found": False, "summary": None, "sources": []}


_MAPS_API_KEY = os.environ["GOOGLE_MAPS_API_KEY"]
_TEXT_SEARCH_URL = "https://places.googleapis.com/v1/places:searchText"
_WEATHER_URL = "https://api.open-meteo.com/v1/forecast"


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
        resp = await _client().get(_WEATHER_URL, params={
            "latitude": lat, "longitude": lon,
            "daily": "weathercode,temperature_2m_max,temperature_2m_min,precipitation_probability_max",
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
        forecast = {
            "found": True,
            "weather_code": daily["weathercode"][idx],
            "temp_max_c": daily["temperature_2m_max"][idx],
            "temp_min_c": daily["temperature_2m_min"][idx],
            "precipitation_probability_max": daily["precipitation_probability_max"][idx],
        }
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
