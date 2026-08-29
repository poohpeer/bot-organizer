import httpx

from bot.ai.proxy import ProxyProvider


class FakeResponse:
    def __init__(self, status=200, body=None):
        self.status_code = status
        self._body = body or {"result": "ok"}

    def raise_for_status(self):
        if self.status_code >= 400:
            request = httpx.Request("POST", "http://ai-proxy/v1/complete")
            response = httpx.Response(self.status_code, request=request, json=self._body)
            raise httpx.HTTPStatusError("bad request", request=request, response=response)

    def json(self):
        return self._body


class FakeClient:
    response = FakeResponse()
    body = None

    def __init__(self, **_kwargs):
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_args):
        pass

    async def post(self, _url, *, json):
        FakeClient.body = json
        return self.response


async def test_routes_prefixed_models_to_their_backend(monkeypatch):
    monkeypatch.setattr("bot.ai.proxy.httpx.AsyncClient", FakeClient)
    provider = ProxyProvider("http://ai-proxy")

    for model, expected_provider, expected_model in (
        # A bare prefix carries no model: codex only runs the ChatGPT
        # account's default and rejects any name, so None must go out — not
        # "", which the proxy would echo back as a model that does not exist.
        ("codex:", "codex", None),
        ("codex:gpt-5.3-codex", "codex", "gpt-5.3-codex"),
        ("ollama:llama3.2", "ollama", "llama3.2"),
        ("claude:opus", "claude_code", "opus"),
        ("openai/gpt-oss-20b", "groq", "openai/gpt-oss-20b"),
        ("gemini-3.6-flash", "gemini", "gemini-3.6-flash"),
    ):
        await provider._request(model, "hello")
        assert FakeClient.body["provider"] == expected_provider
        assert FakeClient.body["model"] == expected_model


async def test_unavailable_proxy_provider_becomes_retryable(monkeypatch):
    FakeClient.response = FakeResponse(
        400, {"error": {"type": "unavailable_provider", "message": "not configured"}}
    )
    monkeypatch.setattr("bot.ai.proxy.httpx.AsyncClient", FakeClient)
    provider = ProxyProvider("http://ai-proxy")

    try:
        await provider._request("claude:opus", "hello")
    except Exception as exc:
        assert exc.status == 503
    else:
        raise AssertionError("expected proxy error")


async def test_unsupported_tool_use_becomes_retryable(monkeypatch):
    """ai-proxy returns this when a backend cannot forward tool declarations
    at all. That says nothing about the request being wrong — only that this
    backend cannot serve it — so the chain must fall through to one that can,
    not surface a 400 to the user. Left non-retryable, a chain whose first
    entry is a CLI-backed provider would fail every tool-using turn outright.
    """
    FakeClient.response = FakeResponse(
        400,
        {"error": {"type": "unsupported_tool_use",
                   "message": "provider codex does not support tool calling"}},
    )
    monkeypatch.setattr("bot.ai.proxy.httpx.AsyncClient", FakeClient)
    provider = ProxyProvider("http://ai-proxy")

    try:
        await provider._request("codex:", "hello")
    except Exception as exc:
        assert exc.status == 503
    else:
        raise AssertionError("expected proxy error")


async def test_a_genuinely_bad_request_stays_non_retryable(monkeypatch):
    """The mapping must stay narrow: a 400 the next provider would also
    reject has to surface, not walk the whole chain repeating it."""
    FakeClient.response = FakeResponse(
        400, {"error": {"type": "missing_schema", "message": "json_schema required"}}
    )
    monkeypatch.setattr("bot.ai.proxy.httpx.AsyncClient", FakeClient)
    provider = ProxyProvider("http://ai-proxy")

    try:
        await provider._request("openai/gpt-oss-120b", "hello")
    except Exception as exc:
        assert exc.status == 400
    else:
        raise AssertionError("expected proxy error")


def test_a_cli_entry_routes_to_its_provider_with_no_model_of_its_own():
    """The CLIs were out of this chain while their adapters accepted a tools
    array and ignored it — live: "start provider=codex tools=26" then
    "complete ... tool_calls=0", and an answer invented rather than read.
    They are back now that they reach the bot's tools over MCP, and the bare
    "codex:" prefix still means "this provider, no model": a ChatGPT-
    authenticated CLI only runs its account's default."""
    import bot.ai.client as client

    assert client.DEFAULT_PROXY_MODELS.startswith("openai/gpt-oss-120b")
    assert "codex:" in client.DEFAULT_PROXY_MODELS
    assert ProxyProvider("http://ai-proxy")._route_model("codex:") == ("codex", None)
    assert ProxyProvider("http://ai-proxy")._route_model("claude:sonnet") == ("claude_code", "sonnet")
