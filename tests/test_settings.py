"""Per-chat settings, and the model chain that is the first of them."""

import bot.ai.client as ai_client
import bot.settings as settings


async def test_a_chat_that_has_never_chosen_gets_the_deployment_default(db_pool):
    assert await settings.get_provider_chain(db_pool, -100) == list(ai_client.PROXY_MODELS)


async def test_a_chosen_order_comes_back(db_pool):
    chosen = list(reversed(ai_client.PROXY_MODELS))

    await settings.set_provider_chain(db_pool, -100, chosen, updated_by=7)

    assert await settings.get_provider_chain(db_pool, -100) == chosen


async def test_one_chat_does_not_change_another(db_pool):
    """Per chat rather than per deployment: one group changing the chain for
    every other group is not a setting, it is a surprise."""
    await settings.set_provider_chain(db_pool, -100, [ai_client.PROXY_MODELS[-1]])

    assert await settings.get_provider_chain(db_pool, -200) == list(ai_client.PROXY_MODELS)


async def test_a_model_the_deployment_no_longer_offers_is_dropped(db_pool):
    """AI_PROXY_MODELS is the source of what exists. A chat that picked
    something a month ago should not be stuck on a chain ai-proxy refuses."""
    await settings.set_provider_chain(
        db_pool, -100, ["gone-from-the-deployment", ai_client.PROXY_MODELS[0]]
    )

    assert await settings.get_provider_chain(db_pool, -100) == [ai_client.PROXY_MODELS[0]]


async def test_a_chain_with_nothing_left_falls_back_to_the_default(db_pool):
    """Better a working default than a chain that cannot answer at all."""
    await settings.set_provider_chain(db_pool, -100, ["gone", "also-gone"])

    assert await settings.get_provider_chain(db_pool, -100) == list(ai_client.PROXY_MODELS)


async def test_resetting_follows_the_default_again_rather_than_copying_it(db_pool):
    """Deleting the row, not storing today's default: the chat should follow
    the deployment if the deployment changes."""
    await settings.set_provider_chain(db_pool, -100, [ai_client.PROXY_MODELS[0]])

    await settings.reset_provider_chain(db_pool, -100)

    stored = await db_pool.fetchval(
        "SELECT count(*) FROM chat_settings WHERE chat_id = -100 AND key = $1",
        settings.PROVIDER_CHAIN,
    )
    assert stored == 0
    assert await settings.get_provider_chain(db_pool, -100) == list(ai_client.PROXY_MODELS)


def _with_a_proxy(monkeypatch):
    """The chain is empty without one, which is correct in production — the
    bot cannot reach a model at all — but says nothing about ordering."""
    monkeypatch.setattr(ai_client, "proxy", object())


def test_each_chat_gets_its_own_sticky_chain(monkeypatch):
    _with_a_proxy(monkeypatch)
    """The switch past a rate-limited model is sticky. A single shared object
    made that knowledge global, so one busy chat pushed every other chat down
    the chain with it."""
    first = ai_client.fallback_for(-100, list(ai_client.PROXY_MODELS))
    second = ai_client.fallback_for(-200, list(ai_client.PROXY_MODELS))

    first.index = 3

    assert second.index == 0
    assert ai_client.fallback_for(-100, list(ai_client.PROXY_MODELS)).index == 3


def test_changing_the_order_rebuilds_the_chain(monkeypatch):
    _with_a_proxy(monkeypatch)
    """And resets the stickiness with it — the reason to stay past a model
    does not apply to a chain it may no longer be in."""
    original = list(ai_client.PROXY_MODELS)
    chain = ai_client.fallback_for(-300, original)
    chain.index = 2

    rebuilt = ai_client.fallback_for(-300, list(reversed(original)))

    assert rebuilt.index == 0
    assert [m for _p, m in rebuilt.chain] == list(reversed(original))


def test_forgetting_a_chat_drops_its_chain(monkeypatch):
    _with_a_proxy(monkeypatch)
    chain = ai_client.fallback_for(-400, list(ai_client.PROXY_MODELS))
    chain.index = 1

    ai_client.forget_chat(-400)

    assert ai_client.fallback_for(-400, list(ai_client.PROXY_MODELS)).index == 0
