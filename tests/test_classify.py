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


def test_the_classifier_never_asks_a_cli_backed_adapter():
    """They are driven through a command-line tool that answers in prose, so a
    request for JSON against a schema comes back empty. Every caller of
    extract() fails closed on {} by design, which turns an empty answer into
    the decision "no" — live, with codex at the front of the chain, the bot
    answered «Что отслеживаем?» to «Едем в парк», «Море» and «Едем на море»
    in turn and could not start a session at all."""
    import bot.ai.client as client

    assert client.CLASSIFIER_MODELS, "the classifier needs somewhere to go"
    assert not [m for m in client.CLASSIFIER_MODELS if client.is_cli_backed(m)]


def test_the_classifier_chain_follows_the_main_one_minus_the_cli_adapters():
    """Derived, not configured separately: reordering AI_PROXY_MODELS is a
    thing operators do, and it must not be able to take the classifier down."""
    import bot.ai.client as client

    expected = [m for m in client.PROXY_MODELS if not client.is_cli_backed(m)]
    assert client.CLASSIFIER_MODELS == expected


async def test_extract_walks_the_classifier_chain_not_the_full_one(monkeypatch):
    import bot.ai.classify as classify_module
    import bot.ai.client as client

    asked = []

    class _Proxy:
        async def _request(self, model, text, **kw):
            asked.append(model)
            return {"structured_output": {"result": True}}

    monkeypatch.setattr(classify_module, "proxy", _Proxy())
    monkeypatch.delenv("AI_PROXY_MODEL", raising=False)

    await classify_module.classify("Is this about food?", "борщ")

    assert asked[0] == client.CLASSIFIER_MODELS[0]
    assert not client.is_cli_backed(asked[0])
