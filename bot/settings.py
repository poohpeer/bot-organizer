"""Per-chat settings, and the one that exists so far: the model chain.

Per chat rather than per deployment because the chain is a routing decision,
and one group changing it for every other group is not a setting, it is a
surprise. A chat that has never touched it uses the deployment default from
AI_PROXY_MODELS, so nothing changes until somebody asks for it.
"""

import json
import logging

import bot.ai.client as ai_client

log = logging.getLogger(__name__)

PROVIDER_CHAIN = "provider_chain"


async def get_provider_chain(pool, chat_id: int) -> list[str]:
    """The models this chat tries, in order. The deployment default when the
    chat has never chosen, and again when its choice has gone stale."""
    stored = await pool.fetchval(
        "SELECT value FROM chat_settings WHERE chat_id = $1 AND key = $2",
        chat_id, PROVIDER_CHAIN,
    )
    if stored is None:
        return list(ai_client.PROXY_MODELS)

    chosen = json.loads(stored) if isinstance(stored, str) else stored
    # Anything the deployment no longer offers is dropped rather than tried:
    # a model removed from AI_PROXY_MODELS is one ai-proxy will refuse, and a
    # chat that picked it a month ago should not be stuck on a dead chain.
    kept = [m for m in chosen if m in ai_client.PROXY_MODELS]
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
