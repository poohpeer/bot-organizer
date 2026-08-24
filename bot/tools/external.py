import logging

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
