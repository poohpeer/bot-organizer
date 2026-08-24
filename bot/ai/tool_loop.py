import logging

from google.genai import types

log = logging.getLogger(__name__)

MAX_TOOL_ITERATIONS = 6


async def run_tool_loop(fallback, prompt, registry: dict, *, history=None, system_instruction=None) -> str:
    """Sends `prompt`, executes any function calls the model returns against
    `registry`, feeds the results back, and repeats until the model returns
    plain text. Raises RuntimeError if it never converges within
    MAX_TOOL_ITERATIONS turns; raises whatever the underlying Gemini call
    raises on API failure. Neither case is caught here — callers apply
    the fixed fallback reply."""
    from bot.tools.schema import ALL_TOOLS

    config = types.GenerateContentConfig(tools=[ALL_TOOLS], system_instruction=system_instruction)
    _model, chat, resp = await fallback.send_message(prompt, config=config, history=history)

    for _ in range(MAX_TOOL_ITERATIONS):
        calls = resp.function_calls
        if not calls:
            return resp.text or ""

        parts = []
        for call in calls:
            fn = registry.get(call.name)
            if fn is None:
                result = {"error": f"unknown tool {call.name!r}"}
            else:
                try:
                    result = await fn(**call.args)
                except Exception as e:  # tool errors become a result, not a crash
                    log.exception("Tool %s raised", call.name)
                    result = {"error": str(e)}
            parts.append(types.Part.from_function_response(name=call.name, response=result))

        resp = await chat.send_message(parts)

    raise RuntimeError(f"tool loop exceeded max iterations ({MAX_TOOL_ITERATIONS})")
