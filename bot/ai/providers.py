"""The provider-neutral conversation surface `bot/ai/tool_loop.py` speaks.

This file used to also hold Groq and Gemini SDK adapters, because the loop
talked to those SDKs directly. It no longer does: every model call in this
bot goes through ai-proxy over HTTP (see `bot/ai/proxy.py`), which is the one
place that knows any provider's dialect. What survives here is the small
vocabulary the loop is written against:

    chat = await provider.start(model, prompt, tools=…, system_instruction=…)
    reply = chat.reply                  # .text and .tool_calls
    reply = await chat.send_tool_results([(call, result_dict), …])
    chat.history()                      # opaque; replayable on another model

Deliberately no SDK imports and no API keys. The adapters that carried them
were dead — nothing constructed a Groq or Gemini client anywhere in the bot —
and leaving them in implied a direct path to the model providers that does
not exist.
"""

from dataclasses import dataclass, field


@dataclass
class ToolCall:
    name: str
    args: dict
    id: str | None = None


@dataclass
class Reply:
    text: str
    tool_calls: list[ToolCall] = field(default_factory=list)
