import json
import logging
import time

from google.genai import errors, types

from bot.ai.client import CLASSIFIER_CHAIN, CLASSIFIER_MODEL, gemini, groq_client
from bot.ai.providers import is_retryable
from bot.logging_setup import truncate

log = logging.getLogger(__name__)

_BOOL_SCHEMA = types.Schema(
    type=types.Type.OBJECT,
    properties={"result": types.Schema(type=types.Type.BOOLEAN)},
    required=["result"],
)


def _json_schema_of(schema) -> dict:
    """Groq wants a JSON Schema; our declarations are written in Gemini's."""
    from bot.ai.tool_schema_openai import _TYPES

    return {
        "type": "object",
        "properties": {
            name: {"type": _TYPES[prop.type]} for name, prop in (schema.properties or {}).items()
        },
        "required": list(schema.required or []),
        "additionalProperties": False,
    }


async def _extract_via_chain(instruction: str, text: str, schema) -> str | None:
    """Ask the cheap models in order, moving on for the same reasons the main
    chain does. Returns the raw JSON text, or None if nobody answered."""
    last_error = None
    for provider, model in CLASSIFIER_CHAIN:
        try:
            if provider.name == "groq":
                resp = await groq_client.chat.completions.create(
                    model=model,
                    messages=[{"role": "system", "content": instruction},
                              {"role": "user", "content": text}],
                    response_format={"type": "json_schema", "json_schema": {
                        "name": "extraction", "strict": True, "schema": _json_schema_of(schema)}},
                )
                return resp.choices[0].message.content
            resp = await gemini.aio.models.generate_content(
                model=model,
                contents=text,
                config=types.GenerateContentConfig(
                    system_instruction=instruction,
                    response_mime_type="application/json",
                    response_schema=schema,
                    automatic_function_calling=types.AutomaticFunctionCallingConfig(disable=True),
                ),
            )
            return resp.text
        except Exception as e:
            if not is_retryable(e):
                raise
            last_error = e
            log.warning("classifier %s/%s unavailable (%s), trying the next", provider.name, model, e)
    log.warning("every classifier model refused; last error: %s", last_error)
    return None


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
        raw = await _extract_via_chain(instruction, text, schema)
        # An empty completion is not JSON. json.loads(None) raises TypeError,
        # which is not a ValueError and would escape a narrower handler.
        if not raw:
            log.warning("extract() got an empty response, defaulting to {}")
            return {}
        log.debug("AI extract <- %.0fms | %s",
                  (time.perf_counter() - started) * 1000, truncate(raw))
        parsed = json.loads(raw)
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
