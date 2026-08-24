import json
import logging

from google.genai import errors, types

from bot.ai.client import CLASSIFIER_MODEL, gemini

log = logging.getLogger(__name__)

_BOOL_SCHEMA = types.Schema(
    type=types.Type.OBJECT,
    properties={"result": types.Schema(type=types.Type.BOOLEAN)},
    required=["result"],
)


async def extract(instruction: str, text: str, schema: types.Schema) -> dict:
    """Cheap structured-JSON extraction against CLASSIFIER_MODEL. Fails
    closed (returns {}) on any error — a classifier failure must never
    cause a proactive suggestion, a session start/stop, or an active-mode
    tool call to fire.

    "Any error" is meant literally, hence the bare `except Exception`:
    besides APIError this has to absorb transport failures (httpx
    ConnectError/ReadTimeout, which are not APIError subclasses) and
    malformed-response failures. Anything that escapes here would
    propagate into the dormant-mode filter and the session start/stop
    paths, turning a transient blip into a wrong action.
    """
    try:
        resp = await gemini.aio.models.generate_content(
            model=CLASSIFIER_MODEL,
            contents=text,
            config=types.GenerateContentConfig(
                system_instruction=instruction,
                response_mime_type="application/json",
                response_schema=schema,
            ),
        )
        # resp.text is None whenever the response carries no text parts —
        # a safety-blocked candidate, or one truncated at MAX_TOKENS before
        # emitting text. json.loads(None) would raise TypeError, which is
        # not a ValueError and would escape a narrower handler.
        if not resp.text:
            log.warning("extract() got an empty response, defaulting to {}")
            return {}

        parsed = json.loads(resp.text)
        # A model is not obliged to honor response_schema; a JSON array or
        # scalar parses fine but has no .get(), which would blow up in
        # classify() instead of failing closed here.
        if not isinstance(parsed, dict):
            log.warning("extract() got non-object JSON (%s), defaulting to {}", type(parsed).__name__)
            return {}
        return parsed
    except Exception:
        log.warning("extract() failed, defaulting to empty result", exc_info=True)
        return {}


async def classify(instruction: str, text: str) -> bool:
    result = await extract(instruction, text, _BOOL_SCHEMA)
    return bool(result.get("result", False))
