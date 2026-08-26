"""One conversation interface over two very different SDKs.

`bot/ai/tool_loop.py` used to speak Gemini's own objects — chat sessions,
`response.function_calls`, `Part.from_function_response`. Adding Groq in front
of Gemini means the loop can no longer be written against either SDK, so both
are adapted to the small surface the loop actually needs:

    chat = await provider.start(model, prompt, tools=…, system_instruction=…)
    reply = chat.reply                  # .text and .tool_calls
    reply = await chat.send_tool_results([(call, result_dict), …])
    chat.history()                      # opaque; replayable on another provider

Only `history()` crosses provider boundaries, so it is provider-neutral: a list
of plain dicts that each provider renders into its own format. That is what
lets a conversation that started on Groq finish on Gemini after a 429.
"""

import json
import logging
import os
from dataclasses import dataclass, field

import groq
from google.genai import errors as gemini_errors
from google.genai import types

from google import genai

log = logging.getLogger(__name__)


@dataclass
class ToolCall:
    name: str
    args: dict
    id: str | None = None


@dataclass
class Reply:
    text: str
    tool_calls: list[ToolCall] = field(default_factory=list)


# A bad or missing key makes a provider unusable, but says nothing about the
# next one. Treating it as fatal would let one misconfigured key take down a
# bot that could have run on the other provider — and silently, since the
# classifier fails closed.
_PROVIDER_UNUSABLE = (401, 403)

# Groq returns 400 output_parse_failed when it cannot parse the tool call its
# own model emitted. Observed on openai/gpt-oss-20b in 3 of 6 tool-calling
# runs. It is a 400, but it says nothing about our request — the same request
# succeeds on the next attempt or the next model — so treating it as fatal
# would answer half of those turns with "не понял, переформулируй", which is
# both wrong and unactionable for the user.
_MODEL_MISBEHAVED = "output_parse_failed"


def _groq_error_code(exc) -> str | None:
    body = getattr(exc, "body", None)
    if isinstance(body, dict):
        error = body.get("error")
        if isinstance(error, dict):
            return error.get("code")
    return None


def is_retryable(exc) -> bool:
    """Whether another model deserves a try.

    429 and 5xx are the shared-quota and overload cases the chain exists for,
    and 401/403 mean this provider is unusable rather than this request being
    wrong. A 400 is the request itself, and repeating it elsewhere just burns
    another call to get the same answer.
    """
    if isinstance(exc, groq.APIStatusError):
        if _groq_error_code(exc) == _MODEL_MISBEHAVED:
            log.warning("Model produced output the provider could not parse; trying the next model")
            return True
        if exc.status_code in _PROVIDER_UNUSABLE:
            log.error("Provider rejected our credentials (%s) — check GROQ_API_KEY", exc.status_code)
            return True
        return exc.status_code == 429 or exc.status_code >= 500
    if isinstance(exc, (groq.APIConnectionError, groq.APITimeoutError)):
        return True
    if isinstance(exc, gemini_errors.APIError):
        if exc.code in _PROVIDER_UNUSABLE:
            log.error("Provider rejected our credentials (%s) — check GEMINI_API_KEY", exc.code)
            return True
        # `code` is None when the error body carries no numeric code, and the
        # SDK does not guarantee it is an int. Comparing a str to 500 would
        # raise TypeError from inside an exception handler and bury the real
        # failure, so check the type rather than only for None.
        return exc.code == 429 or (isinstance(exc.code, int) and exc.code >= 500)
    return False


# --- Groq -------------------------------------------------------------------

class _GroqChat:
    def __init__(self, client, model, messages, tools):
        self.client = client
        self.model = model
        self.messages = messages
        self.tools = tools
        self.reply = None

    async def _complete(self) -> Reply:
        response = await self.client.chat.completions.create(
            model=self.model,
            messages=self.messages,
            tools=self.tools or None,
        )
        choice = response.choices[0].message
        calls = [
            ToolCall(name=c.function.name, args=json.loads(c.function.arguments or "{}"), id=c.id)
            for c in (choice.tool_calls or [])
        ]
        # The assistant turn must go back into the history verbatim, tool calls
        # included: Groq rejects a tool result whose call it cannot find.
        entry = {"role": "assistant", "content": choice.content or ""}
        if choice.tool_calls:
            entry["tool_calls"] = [
                {"id": c.id, "type": "function",
                 "function": {"name": c.function.name, "arguments": c.function.arguments}}
                for c in choice.tool_calls
            ]
        self.messages.append(entry)
        self.reply = Reply(text=choice.content or "", tool_calls=calls)
        return self.reply

    async def send_tool_results(self, results) -> Reply:
        for call, result in results:
            self.messages.append({
                "role": "tool",
                "tool_call_id": call.id,
                "content": json.dumps(result, ensure_ascii=False, default=str),
            })
        return await self._complete()

    def history(self) -> list[dict]:
        return list(self.messages)


class GroqProvider:
    name = "groq"

    def __init__(self, client):
        self.client = client

    async def start(self, model, prompt, *, tools=None, system_instruction=None, history=None):
        messages = list(history or [])
        if system_instruction and not any(m["role"] == "system" for m in messages):
            messages.insert(0, {"role": "system", "content": system_instruction})
        if prompt is not None:
            messages.append({"role": "user", "content": prompt})
        chat = _GroqChat(self.client, model, messages, tools)
        await chat._complete()
        return chat


# --- Gemini -----------------------------------------------------------------

def _gemini_contents(history: list[dict]) -> list[types.Content]:
    """Render neutral history into Gemini turns.

    Tool results are replayed as plain text rather than as function-response
    parts: after a provider switch the original call ids mean nothing to
    Gemini, and what matters is that the model can see what the tools returned.
    """
    contents = []
    for message in history:
        role = message["role"]
        if role == "system":
            continue
        if role == "tool":
            contents.append(types.Content(
                role="user",
                parts=[types.Part(text=f"[tool result] {message['content']}")],
            ))
            continue
        text = message.get("content") or ""
        if message.get("tool_calls"):
            named = ", ".join(c["function"]["name"] for c in message["tool_calls"])
            text = f"{text} [called: {named}]".strip()
        if not text:
            continue
        contents.append(types.Content(
            role="model" if role == "assistant" else "user",
            parts=[types.Part(text=text)],
        ))
    return contents


class _GeminiChat:
    def __init__(self, chat, messages):
        self.chat = chat
        self.messages = messages
        self.reply = None

    def _record(self, resp) -> Reply:
        calls = [ToolCall(name=c.name, args=dict(c.args or {})) for c in (resp.function_calls or [])]
        entry = {"role": "assistant", "content": resp.text or ""}
        if calls:
            entry["tool_calls"] = [
                {"id": f"gemini-{i}", "type": "function",
                 "function": {"name": c.name, "arguments": json.dumps(c.args, default=str)}}
                for i, c in enumerate(calls)
            ]
        self.messages.append(entry)
        self.reply = Reply(text=resp.text or "", tool_calls=calls)
        return self.reply

    async def send_tool_results(self, results) -> Reply:
        parts = []
        for call, result in results:
            self.messages.append({
                "role": "tool", "tool_call_id": call.id,
                "content": json.dumps(result, ensure_ascii=False, default=str),
            })
            parts.append(types.Part.from_function_response(name=call.name, response=result))
        return self._record(await self.chat.send_message(parts))

    def history(self) -> list[dict]:
        return list(self.messages)


class GeminiProvider:
    name = "gemini"

    def __init__(self, client, tools_declaration):
        self.client = client
        self.tools_declaration = tools_declaration

    async def start(self, model, prompt, *, tools=None, system_instruction=None, history=None):
        messages = list(history or [])
        outgoing = prompt
        replay = messages

        if prompt is None:
            # Re-driving a conversation that began on another provider: there
            # is no new user message, only tool results already in `messages`.
            # A Gemini chat still has to be *sent* something, so the trailing
            # tool results become the outgoing turn rather than sitting in the
            # replayed history, where nothing would prompt a response.
            split = len(messages)
            while split and messages[split - 1]["role"] == "tool":
                split -= 1
            replay, trailing = messages[:split], messages[split:]
            outgoing = (
                "\n".join(f"[tool result] {m['content']}" for m in trailing)
                if trailing else "Continue."
            )

        config = types.GenerateContentConfig(
            tools=[self.tools_declaration] if tools else None,
            system_instruction=system_instruction,
        )
        chat = self.client.aio.chats.create(
            model=model, config=config, history=_gemini_contents(replay)
        )
        if prompt is not None:
            messages.append({"role": "user", "content": prompt})
        wrapper = _GeminiChat(chat, messages)
        wrapper._record(await chat.send_message(outgoing))
        return wrapper


def build_groq_client():
    """The Groq client, or None when no key is configured.

    Deliberately not the crash-on-missing-env treatment the other keys get.
    Groq sits *in front of* Gemini in the chain: it makes the bot more
    resilient, it is not what the bot is built on. Refusing to start without it
    would turn an optional accelerant into a hard dependency and take down a
    deployment that was working fine yesterday.
    """
    key = os.environ.get("GROQ_API_KEY")
    if not key:
        log.warning("GROQ_API_KEY is not set — running on Gemini alone")
        return None
    return groq.AsyncGroq(api_key=key)


def build_gemini_client():
    return genai.Client(api_key=os.environ["GEMINI_API_KEY"])
