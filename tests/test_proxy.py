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
