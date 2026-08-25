import json
import logging
import time

from google.genai import errors, types

from bot.ai.client import CLASSIFIER_MODEL, gemini
from bot.logging_setup import truncate

log = logging.getLogger(__name__)

_BOOL_SCHEMA = types.Schema(
    type=types.Type.OBJECT,
    properties={"result": types.Schema(type=types.Type.BOOLEAN)},
    required=["result"],
)


async def extract(instruction: str, text: str, schema: types.Schema) -> dict:
    """Cheap structured-JSON extraction against CLASSIFIER_MODEL. Fails
    closed (returns {}) on any error — a classifier failure must never
    cause a session start/stop or an active-mode
    tool call to fire.

    "Any error" is meant literally, hence the bare `except Exception`:
    besides APIError this has to absorb transport failures (httpx
    ConnectError/ReadTimeout, which are not APIError subclasses) and
    malformed-response failures. Anything that escapes here would
    propagate into the dormant-mode filter and the session start/stop
    paths, turning a transient blip into a wrong action.
    """
    started = time.perf_counter()
    log.debug("AI extract -> %s | instruction=%s | text=%s",
              CLASSIFIER_MODEL, truncate(instruction, 120), truncate(text))
    try:
        resp = await gemini.aio.models.generate_content(
            model=CLASSIFIER_MODEL,
            contents=text,
            config=types.GenerateContentConfig(
                system_instruction=instruction,
                response_mime_type="application/json",
                response_schema=schema,
                # generate_content takes the SDK's automatic-function-calling
                # path unless told otherwise — regardless of whether any tools
                # were supplied — and logs a warning recommending a chat
                # session. This call returns structured JSON and declares no
                # tools at all, so there is nothing to call back into. The one
                # place that does use function calling, bot/ai/tool_loop.py,
                # already goes through AsyncChat as the SDK recommends.
                automatic_function_calling=types.AutomaticFunctionCallingConfig(disable=True),
            ),
        )
        # resp.text is None whenever the response carries no text parts —
        # a safety-blocked candidate, or one truncated at MAX_TOKENS before
        # emitting text. json.loads(None) would raise TypeError, which is
        # not a ValueError and would escape a narrower handler.
        if not resp.text:
            log.warning("extract() got an empty response, defaulting to {}")
            return {}

        log.debug("AI extract <- %.0fms | %s",
                  (time.perf_counter() - started) * 1000, truncate(resp.text))
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
