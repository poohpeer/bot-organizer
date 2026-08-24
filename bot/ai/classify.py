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
    tool call to fire."""
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
        return json.loads(resp.text)
    except (errors.APIError, ValueError):
        log.warning("extract() failed, defaulting to {}", exc_info=True)
        return {}


async def classify(instruction: str, text: str) -> bool:
    result = await extract(instruction, text, _BOOL_SCHEMA)
    return bool(result.get("result", False))
