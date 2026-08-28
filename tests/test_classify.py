from google.genai import types

from bot.ai import classify as classify_module


class _Proxy:
    async def _request(self, model, prompt, **kwargs):
        return {"structured_output": {"result": prompt == "let's get pizza"}}


async def test_classify_uses_proxy(monkeypatch):
    monkeypatch.setattr(classify_module, "proxy", _Proxy())
    assert await classify_module.classify("Is this about food?", "let's get pizza") is True
    assert await classify_module.classify("Is this about food?", "nice weather today") is False


async def test_extract_returns_proxy_json(monkeypatch):
    class Proxy:
        async def _request(self, *args, **kwargs):
            return {"structured_output": {"is_start": True, "activity_type": "picnic"}}
    monkeypatch.setattr(classify_module, "proxy", Proxy())
    schema = types.Schema(type=types.Type.OBJECT, properties={})
    assert await classify_module.extract("...", "picnic", schema) == {
        "is_start": True, "activity_type": "picnic"
    }


async def test_extract_fails_closed_when_proxy_fails(monkeypatch):
    class Proxy:
        async def _request(self, *args, **kwargs):
            raise RuntimeError("connection refused")
    monkeypatch.setattr(classify_module, "proxy", Proxy())
    schema = types.Schema(type=types.Type.OBJECT, properties={})
    assert await classify_module.extract("...", "anything", schema) == {}


def test_proxy_schema_is_openai_json_schema():
    schema = types.Schema(type=types.Type.OBJECT, properties={
        "result": types.Schema(type=types.Type.BOOLEAN),
    })
    rendered = classify_module._json_schema_of(schema)
    assert rendered["type"] == "object"
    assert rendered["required"] == ["result"]
