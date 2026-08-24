import logging
import os

import httpx
from google.genai import errors, types

from bot.ai.client import MODELS, gemini

log = logging.getLogger(__name__)


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
    """Grounded search via Gemini's built-in Google Search tool. Never
    invents an answer: an empty result or an API failure both come back
    as found=False rather than falling through to the model's own
    (possibly stale or wrong) knowledge."""
    try:
        resp = await gemini.aio.models.generate_content(
            model=MODELS[0],
            contents=query,
            config=types.GenerateContentConfig(
                tools=[types.Tool(google_search=types.GoogleSearch())]
            ),
        )
    except errors.APIError:
        log.warning("web_search failed for query=%r", query, exc_info=True)
        return {"found": False, "summary": None, "sources": []}

    text = (resp.text or "").strip()
    if not text:
        return {"found": False, "summary": None, "sources": []}
    return {"found": True, "summary": text, "sources": _extract_sources(resp)}


_MAPS_API_KEY = os.environ["GOOGLE_MAPS_API_KEY"]
_TEXT_SEARCH_URL = "https://places.googleapis.com/v1/places:searchText"
_WEATHER_URL = "https://api.open-meteo.com/v1/forecast"
_HTTP_TIMEOUT = 10.0


async def maps_lookup(query: str) -> dict:
    """Text-search a place via Google Places. Any failure mode — non-200,
    a transport error, unparsable JSON, or a response missing the fields
    we need — comes back as found=False rather than raising, so a flaky
    upstream never crashes the tool loop or produces a half-built answer."""
    try:
        async with httpx.AsyncClient(timeout=_HTTP_TIMEOUT) as client:
            resp = await client.post(
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

    return {
        "found": True,
        "name": (p.get("displayName") or {}).get("text"),
        "address": p.get("formattedAddress"),
        "lat": lat,
        "lon": lon,
        "rating": p.get("rating"),
        "review_snippets": [
            (r.get("text") or {}).get("text")
            for r in (p.get("reviews") or [])[:5]
            if isinstance(r, dict)
        ],
    }


async def weather_lookup(lat: float, lon: float, date: str) -> dict:
    """Daily forecast via Open-Meteo. Same fail-safe contract as
    maps_lookup: non-200, transport errors, malformed JSON, or a response
    missing the requested date's fields all resolve to found=False."""
    try:
        async with httpx.AsyncClient(timeout=_HTTP_TIMEOUT) as client:
            resp = await client.get(_WEATHER_URL, params={
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
        return {
            "found": True,
            "weather_code": daily["weathercode"][idx],
            "temp_max_c": daily["temperature_2m_max"][idx],
            "temp_min_c": daily["temperature_2m_min"][idx],
            "precipitation_probability_max": daily["precipitation_probability_max"][idx],
        }
    except (KeyError, IndexError, TypeError):
        log.warning("weather_lookup response missing expected daily fields for date=%r", date, exc_info=True)
        return {"found": False}


def build_external_registry() -> dict:
    return {
        "web_search": web_search,
        "maps_lookup": maps_lookup,
        "weather_lookup": weather_lookup,
    }
