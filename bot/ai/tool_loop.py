import logging
import time

from bot.ai.client import AllModelsUnavailable
from bot.ai.proxy import is_retryable
from bot.ai.tool_schema_openai import openai_tools
from bot.logging_setup import truncate

log = logging.getLogger(__name__)

MAX_TOOL_ITERATIONS = 6


def _as_response_dict(result) -> dict:
    """Wrap a tool's return value so it is always a JSON object.

    Several tools naturally return sequences (list_show, get_participants,
    archive_lookup), and both providers want an object for a tool result.
    """
    return result if isinstance(result, dict) else {"result": result}


async def _call_tool(registry: dict, call) -> dict:
    fn = registry.get(call.name)
    if fn is None:
        log.warning("Model called unknown tool %r", call.name)
        return {"error": f"unknown tool {call.name!r}"}
    started = time.perf_counter()
    log.debug("tool -> %s(%s)", call.name, truncate(call.args or {}, 200))
    try:
        result = _as_response_dict(await fn(**(call.args or {})))
    except Exception as e:  # tool errors become a result, not a crash
        log.exception("Tool %s raised after %.0fms", call.name, (time.perf_counter() - started) * 1000)
        return {"error": str(e)}
    log.debug("tool <- %s %.0fms | %s", call.name,
              (time.perf_counter() - started) * 1000, truncate(result))
    return result


async def run_tool_loop(fallback, prompt, registry: dict, *, history=None, system_instruction=None) -> str:
    """Sends `prompt`, runs whatever tools the model asks for, feeds the
    results back, and repeats until it answers in plain text.

    Raises AllModelsUnavailable when every model in the chain is refusing, and
    RuntimeError if the model never stops calling tools. Neither is caught
    here — the router turns the first into a specific reply and the second into
    its generic one.
    """
    tools = openai_tools()
    started = time.perf_counter()
    log.debug("tool loop -> prompt=%s | history=%d turns", truncate(prompt), len(history or []))

    provider, model, chat = await fallback.start(
        prompt, tools=tools, system_instruction=system_instruction, history=history
    )
    log.debug("tool loop: first turn answered by %s/%s", provider.name, model)
    reply = chat.reply

    for turn in range(1, MAX_TOOL_ITERATIONS + 1):
        if not reply.tool_calls:
            log.debug("tool loop <- %.0fms after %d turn(s) | %s",
                      (time.perf_counter() - started) * 1000, turn, truncate(reply.text))
            return reply.text

        log.debug("tool loop turn %d: model requested %s",
                  turn, ", ".join(c.name for c in reply.tool_calls))
        results = [(call, await _call_tool(registry, call)) for call in reply.tool_calls]

        try:
            reply = await chat.send_tool_results(results)
        except Exception as e:
            # Only the opening turn went through the chain. A 429 on a later
            # turn is if anything more likely — quota runs out mid-conversation
            # — so re-drive what has been said so far on the next model rather
            # than losing the whole exchange.
            if not is_retryable(e):
                raise
            log.warning("Tool loop turn failed (%s), re-driving on the next model", e)
            fallback.index += 1
            provider, model, chat = await fallback.start(
                None, tools=tools, system_instruction=system_instruction, history=chat.history()
            )
            log.debug("tool loop: recovered on %s/%s", provider.name, model)
            reply = chat.reply

    if not reply.tool_calls:
        log.debug("tool loop <- %.0fms at the iteration limit | %s",
                  (time.perf_counter() - started) * 1000, truncate(reply.text))
        return reply.text

    log.warning("Tool loop still calling tools after %d turns, giving up", MAX_TOOL_ITERATIONS)
    raise RuntimeError(f"tool loop exceeded max iterations ({MAX_TOOL_ITERATIONS})")
