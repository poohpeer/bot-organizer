import logging

from google.genai import errors, types

log = logging.getLogger(__name__)

MAX_TOOL_ITERATIONS = 6


def _as_response_dict(result) -> dict:
    """Wrap a tool's return value so it is always a JSON object.

    `Part.from_function_response(response=...)` requires a dict — handing it
    a list or scalar raises a pydantic ValidationError that would abort the
    whole loop and lose the reply. Several tools naturally return sequences
    (list_show, get_participants, archive_lookup), so normalize here rather
    than constraining every tool's return type.
    """
    return result if isinstance(result, dict) else {"result": result}


async def _call_tool(registry: dict, call) -> dict:
    fn = registry.get(call.name)
    if fn is None:
        return {"error": f"unknown tool {call.name!r}"}
    try:
        return _as_response_dict(await fn(**(call.args or {})))
    except Exception as e:  # tool errors become a result, not a crash
        log.exception("Tool %s raised", call.name)
        return {"error": str(e)}


async def run_tool_loop(fallback, prompt, registry: dict, *, history=None, system_instruction=None) -> str:
    """Sends `prompt`, executes any function calls the model returns against
    `registry`, feeds the results back, and repeats until the model returns
    plain text. Raises RuntimeError if it never converges within
    MAX_TOOL_ITERATIONS tool rounds; raises whatever the underlying Gemini
    call raises on API failure. Neither case is caught here — callers apply
    the fixed fallback reply.
    """
    from bot.tools.schema import ALL_TOOLS

    config = types.GenerateContentConfig(tools=[ALL_TOOLS], system_instruction=system_instruction)
    _model, chat, resp = await fallback.send_message(prompt, config=config, history=history)

    for _ in range(MAX_TOOL_ITERATIONS):
        calls = resp.function_calls
        if not calls:
            return resp.text or ""

        parts = [
            types.Part.from_function_response(name=call.name, response=await _call_tool(registry, call))
            for call in calls
        ]

        try:
            resp = await chat.send_message(parts)
        except errors.APIError as e:
            # Only the first turn went through ModelFallback; a 429/5xx on a
            # later turn would otherwise abort the request outright, even
            # though later turns are exactly when the shared key's quota is
            # most likely to run out. Re-drive the conversation on the next
            # model, preserving the history built so far.
            is_retryable = e.code == 429 or (e.code is not None and e.code >= 500)
            if not (is_retryable and fallback.index < len(fallback.models) - 1):
                raise
            log.warning("Tool loop turn failed (%s), retrying on next model", e.code)
            fallback.index += 1
            _model, chat, resp = await fallback.send_message(
                parts, config=config, history=chat.get_history()
            )

    # The final round's response may itself be plain text — checking it here
    # rather than raising means a conversation that legitimately uses all
    # MAX_TOOL_ITERATIONS rounds still returns its answer.
    if not resp.function_calls:
        return resp.text or ""

    raise RuntimeError(f"tool loop exceeded max iterations ({MAX_TOOL_ITERATIONS})")
