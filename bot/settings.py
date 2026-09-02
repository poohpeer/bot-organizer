"""Per-chat settings: the model chain, and how strictly to stay on topic.

Per chat rather than per deployment because both are decisions one group can
reasonably make differently from another, and one group changing it for every
other group is not a setting, it is a surprise. A chat that has never touched
either uses the deployment default, so nothing changes until somebody asks.
"""

import json
import logging

import bot.ai.client as ai_client

log = logging.getLogger(__name__)

PROVIDER_CHAIN = "provider_chain"
TOPIC_GUARD = "topic_guard"

# What the bot does with a message that has nothing to do with the event.
TOPIC_GUARD_STRICT = "strict"   # say so, and answer nothing else
TOPIC_GUARD_OFF = "off"         # answer anything, as it did before the guard
TOPIC_GUARD_VALUES = (TOPIC_GUARD_STRICT, TOPIC_GUARD_OFF)

# Strict by default, including for every chat that predates this setting.
# The guard exists because the bot answered a pasta recipe in a group
# organizing a picnic; leaving it off until asked would ship the bug.
TOPIC_GUARD_DEFAULT = TOPIC_GUARD_STRICT


async def get_stored_chain(pool, chat_id: int) -> list[str] | None:
    """What this chat actually chose, or None if it never has.

    Distinct from get_provider_chain, which answers "what will be tried" and
    so cannot tell a chat that picked nothing from one that picked the
    default. The settings menu needs the difference: it shows an empty
    selection as empty, and says which default is standing in.
    """
    stored = await pool.fetchval(
        "SELECT value FROM chat_settings WHERE chat_id = $1 AND key = $2",
        chat_id, PROVIDER_CHAIN,
    )
    if stored is None:
        return None
    chosen = json.loads(stored) if isinstance(stored, str) else stored
    # Anything the deployment no longer offers is dropped rather than tried:
    # a model removed from AI_PROXY_MODELS is one ai-proxy will refuse, and a
    # chat that picked it a month ago should not be stuck on a dead chain.
    return [m for m in chosen if m in ai_client.PROXY_MODELS]


async def get_provider_chain(pool, chat_id: int) -> list[str]:
    """The models this chat tries, in order. The deployment default when the
    chat has never chosen, and again when its choice has gone stale."""
    kept = await get_stored_chain(pool, chat_id)
    if kept is None:
        return list(ai_client.PROXY_MODELS)
    if not kept:
        log.warning("Chat %s has a stored chain with nothing usable left; using the default", chat_id)
        return list(ai_client.PROXY_MODELS)
    return kept


async def set_provider_chain(pool, chat_id: int, models: list[str], *, updated_by: int | None = None) -> None:
    await pool.execute(
        """
        INSERT INTO chat_settings (chat_id, key, value, updated_by)
        VALUES ($1, $2, $3, $4)
        ON CONFLICT (chat_id, key)
        DO UPDATE SET value = EXCLUDED.value, updated_at = now(), updated_by = EXCLUDED.updated_by
        """,
        chat_id, PROVIDER_CHAIN, json.dumps(models), updated_by,
    )


async def reset_provider_chain(pool, chat_id: int) -> None:
    """Back to the deployment default. Deleting rather than storing a copy of
    it, so the chat follows the default again if the default changes."""
    await pool.execute(
        "DELETE FROM chat_settings WHERE chat_id = $1 AND key = $2", chat_id, PROVIDER_CHAIN
    )


async def get_topic_guard(pool, chat_id: int) -> str:
    """How this chat treats a message that is not about the event.

    Never raises and never returns something the caller has to validate: a
    value written by a newer version, or by hand, falls back to the default
    rather than breaking a turn that was otherwise fine.
    """
    stored = await pool.fetchval(
        "SELECT value FROM chat_settings WHERE chat_id = $1 AND key = $2",
        chat_id, TOPIC_GUARD,
    )
    if stored is None:
        return TOPIC_GUARD_DEFAULT
    value = json.loads(stored) if isinstance(stored, str) else stored
    if value not in TOPIC_GUARD_VALUES:
        log.warning("Chat %s has unknown topic_guard %r; using %s",
                    chat_id, value, TOPIC_GUARD_DEFAULT)
        return TOPIC_GUARD_DEFAULT
    return value


async def set_topic_guard(pool, chat_id: int, value: str, *, updated_by: int | None = None) -> bool:
    """Returns False for anything that is not one of TOPIC_GUARD_VALUES, so a
    typo is refused at the point it is made rather than stored as a value
    get_topic_guard will silently ignore later."""
    if value not in TOPIC_GUARD_VALUES:
        return False
    await pool.execute(
        """
        INSERT INTO chat_settings (chat_id, key, value, updated_by)
        VALUES ($1, $2, $3, $4)
        ON CONFLICT (chat_id, key)
        DO UPDATE SET value = EXCLUDED.value, updated_at = now(), updated_by = EXCLUDED.updated_by
        """,
        chat_id, TOPIC_GUARD, json.dumps(value), updated_by,
    )
    return True


async def reset_topic_guard(pool, chat_id: int) -> None:
    """Back to the deployment default, and following it if it ever changes."""
    await pool.execute(
        "DELETE FROM chat_settings WHERE chat_id = $1 AND key = $2", chat_id, TOPIC_GUARD
    )
