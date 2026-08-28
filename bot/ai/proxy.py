"""Generic client for the shared ai-proxy service."""

import json
import os

import httpx

from bot.ai.providers import Reply, ToolCall


class ProxyProvider:
    name = "ai-proxy"

    def __init__(self, endpoint: str, backend_provider: str = "groq"):
        self.endpoint = endpoint.rstrip("/")
        self.backend_provider = backend_provider

    async def _request(self, model, prompt, *, tools=None, system_instruction=None, history=None, output_format="text", schema=None):
        provider, upstream_model = self._route_model(model)
        body = {
            "provider": provider,
            "model": upstream_model,
            "prompt": prompt,
            "system": system_instruction,
            "history": history or [],
            "tools": tools or [],
            "output_format": output_format,
            "json_schema": schema,
        }
        async with httpx.AsyncClient(timeout=float(os.environ.get("AI_PROXY_TIMEOUT_S", "600")) + 5) as client:
            response = await client.post(f"{self.endpoint}/v1/complete", json=body)
            try:
                response.raise_for_status()
            except httpx.HTTPStatusError as exc:
                error_type = None
                try:
                    error_type = exc.response.json().get("error", {}).get("type")
                except (ValueError, AttributeError):
                    pass
                # A shared proxy can legitimately have optional backends
                # absent (for example no Claude binary in this deployment).
                # Make those typed configuration errors retryable so the
                # model chain can continue to the next backend.
                retryable_status = exc.response.status_code
                if error_type in {"unavailable_provider", "missing_api_key"}:
                    retryable_status = 503
                raise ProxyError(retryable_status, str(exc)) from exc
            return response.json()

    def _route_model(self, model):
        """Map a chain model name to the proxy provider and its model name."""
        for prefix, provider in (
            ("codex:", "codex"),
            ("ollama:", "ollama"),
            ("claude:", "claude_code"),
        ):
            if model.startswith(prefix):
                return provider, model[len(prefix):]
        if model.startswith("openai/"):
            return "groq", model
        return self.backend_provider if self.backend_provider != "groq" else "gemini", model

    async def start(self, model, prompt, *, tools=None, system_instruction=None, history=None):
        chat = _ProxyChat(self, model, list(history or []), tools, system_instruction)
        if system_instruction and not any(m.get("role") == "system" for m in chat.messages):
            chat.messages.insert(0, {"role": "system", "content": system_instruction})
        if prompt is not None:
            chat.messages.append({"role": "user", "content": prompt})
        await chat.complete()
        return chat


class _ProxyChat:
    def __init__(self, provider, model, messages, tools, system_instruction):
        self.provider = provider
        self.model = model
        self.messages = messages
        self.tools = tools
        self.system_instruction = system_instruction
        self.reply = None

    async def complete(self):
        prompt = self.messages[-1]["content"] if self.messages and self.messages[-1]["role"] == "user" else "Continue."
        history = self.messages[:-1] if self.messages and self.messages[-1]["role"] == "user" else self.messages
        data = await self.provider._request(
            self.model, prompt, tools=self.tools, history=history,
            system_instruction=self.system_instruction,
        )
        calls = [ToolCall(name=c["name"], args=c.get("arguments", {}), id=c.get("id")) for c in data.get("tool_calls", [])]
        assistant = {"role": "assistant", "content": data.get("result") or ""}
        if calls:
            assistant["tool_calls"] = [{"id": c.id, "type": "function", "function": {
                "name": c.name, "arguments": json.dumps(c.args, ensure_ascii=False)}} for c in calls]
        self.messages.append(assistant)
        self.reply = Reply(text=assistant["content"], tool_calls=calls)
        return self.reply

    async def send_tool_results(self, results):
        for call, result in results:
            self.messages.append({"role": "tool", "tool_call_id": call.id,
                                  "content": json.dumps(result, ensure_ascii=False, default=str)})
        return await self.complete()

    def history(self):
        return list(self.messages)


proxy_endpoint = os.environ.get("AI_PROXY_URL")
proxy = ProxyProvider(proxy_endpoint) if proxy_endpoint else None


class ProxyError(RuntimeError):
    def __init__(self, status, message):
        super().__init__(message)
        self.status = status
