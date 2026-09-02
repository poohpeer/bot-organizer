"""Per-chat settings: the model chain, and the topic guard."""

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


async def test_the_menu_can_tell_never_chosen_from_chose_nothing(db_pool):
    """get_provider_chain answers "what will be tried", so it substitutes the
    default for both and cannot tell them apart. The settings menu has to:
    one draws eight numbered models, the other draws none."""
    await settings.set_provider_chain(db_pool, 2, [])

    assert await settings.get_stored_chain(db_pool, 1) is None
    assert await settings.get_stored_chain(db_pool, 2) == []
    # Both still run on the default — an empty selection is not a dead bot.
    assert await settings.get_provider_chain(db_pool, 1) == list(ai_client.PROXY_MODELS)
    assert await settings.get_provider_chain(db_pool, 2) == list(ai_client.PROXY_MODELS)


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


# --- staying on the event ----------------------------------------------------

async def test_a_chat_that_never_chose_gets_the_guard(db_pool):
    """Strict by default, including for every chat that predates the setting.
    The guard exists because the bot answered a pasta recipe in a group
    organizing a picnic; defaulting to off would ship that bug."""

    assert await settings.get_topic_guard(db_pool, 1) == settings.TOPIC_GUARD_STRICT


async def test_a_chat_can_turn_the_guard_off_and_back_on(db_pool):

    assert await settings.set_topic_guard(db_pool, 1, settings.TOPIC_GUARD_OFF, updated_by=77)
    assert await settings.get_topic_guard(db_pool, 1) == settings.TOPIC_GUARD_OFF

    assert await settings.set_topic_guard(db_pool, 1, settings.TOPIC_GUARD_STRICT)
    assert await settings.get_topic_guard(db_pool, 1) == settings.TOPIC_GUARD_STRICT


async def test_an_unknown_setting_is_refused_rather_than_stored(db_pool):
    """Stored, it would read back as something get_topic_guard ignores — a
    chat believing it had turned the guard off while the guard stayed on."""

    assert await settings.set_topic_guard(db_pool, 1, "loose") is False
    assert await db_pool.fetchval(
        "SELECT count(*) FROM chat_settings WHERE chat_id = 1 AND key = $1", settings.TOPIC_GUARD
    ) == 0


async def test_a_value_written_by_something_else_falls_back(db_pool):
    """A newer version, or a hand-edited row. Falling back beats raising in
    the middle of a turn that was otherwise fine."""
    await db_pool.execute(
        "INSERT INTO chat_settings (chat_id, key, value) VALUES (1, $1, $2)",
        settings.TOPIC_GUARD, '"whatever"',
    )

    assert await settings.get_topic_guard(db_pool, 1) == settings.TOPIC_GUARD_STRICT


async def test_resetting_follows_the_default_again(db_pool):
    await settings.set_topic_guard(db_pool, 1, settings.TOPIC_GUARD_OFF)

    await settings.reset_topic_guard(db_pool, 1)

    assert await db_pool.fetchval(
        "SELECT count(*) FROM chat_settings WHERE chat_id = 1 AND key = $1", settings.TOPIC_GUARD
    ) == 0
    assert await settings.get_topic_guard(db_pool, 1) == settings.TOPIC_GUARD_STRICT
