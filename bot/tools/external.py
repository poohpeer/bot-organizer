import logging
import os
import time

import httpx
from google.genai import types

from bot.ai.client import MODELS
from bot.ai.proxy import ProxyError, proxy
from bot.logging_setup import truncate

log = logging.getLogger(__name__)

_HTTP_TIMEOUT = 10.0

# Grounded search needs a model with a real search tool behind it. Groq's
# GPT-OSS models carry a server-side `browser_search`; Gemini has its own
# Google Search grounding. Gemma has neither, so it is excluded even though it
# is the last resort in the chat chain.
SEARCH_MODELS = [m for m in MODELS if m.startswith("gemini-")]

# Seconds. Without this a hung grounded-search call blocks the tool loop, and
# with it the user's Telegram reply, indefinitely.
_SEARCH_TIMEOUT_MS = 30_000

_shared_client: httpx.AsyncClient | None = None


def _client() -> httpx.AsyncClient:
    """One shared AsyncClient for all outbound HTTP, per the epic's constraint.

    Created lazily so importing this module never needs a running event loop,
    and reused so a search and a lookup in one tool loop don't each pay
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
    last_error = None
    for model in SEARCH_MODELS:
        try:
            data = await proxy._request(model, query, tools=[{"type": "browser_search"}])
            text = (data.get("result") or "").strip()
            if text:
                return {"found": True, "summary": text, "sources": data.get("sources", [])}
        except Exception as exc:
            last_error = exc
            log.warning("web_search via ai-proxy/%s failed", model, exc_info=True)
            status = getattr(exc, "status", None) or getattr(exc, "code", None)
            if status not in (429, 500, 502, 503, 504):
                break
    if last_error:
        log.warning("web_search exhausted proxy models for query=%r", query)
    return {"found": False, "summary": None, "sources": []}


_MAPS_API_KEY = os.environ["GOOGLE_MAPS_API_KEY"]
_TEXT_SEARCH_URL = "https://places.googleapis.com/v1/places:searchText"


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


def build_external_registry() -> dict:
    return {
        "web_search": web_search,
        "maps_lookup": maps_lookup,
    }
