import json
import logging
import os
import time

from google.genai import types

from bot.ai.client import CLASSIFIER_MODEL
from bot.ai.proxy import proxy
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

    def render(node) -> dict:
        rendered = {"type": _TYPES[node.type]}
        if node.description:
            rendered["description"] = node.description
        if getattr(node, "enum", None):
            rendered["enum"] = list(node.enum)
        if node.type == types.Type.ARRAY:
            rendered["items"] = render(node.items)
        if node.type == types.Type.OBJECT:
            properties = {
                name: render(prop) for name, prop in (node.properties or {}).items()
            }
            rendered["properties"] = properties
            # Groq/OpenAI strict JSON schema requires every declared property
            # to be present in `required`. Optional fields are represented by
            # empty strings/arrays at the prompt-contract level instead.
            rendered["required"] = list(properties)
            rendered["additionalProperties"] = False
        return rendered

    return render(schema)


async def _extract_via_chain(instruction: str, text: str, schema) -> str | None:
    """Ask the cheap models in order, moving on for the same reasons the main
    chain does. Returns the raw JSON text, or None if nobody answered."""
    if proxy is None:
        raise RuntimeError("AI_PROXY_URL is not configured")
    data = await proxy._request(
        os.environ.get("AI_PROXY_MODEL", "openai/gpt-oss-120b"), text,
        system_instruction=instruction, output_format="json",
        schema=_json_schema_of(schema),
    )
    return json.dumps(data.get("structured_output")) if isinstance(data.get("structured_output"), dict) else None


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
