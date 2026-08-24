import logging
import re

import bot.decision_log as decision_log
from bot.ai.classify import classify

log = logging.getLogger(__name__)

# Mirrors the CHECK constraint on proactive_suggestions.response. Validated
# here so a wrong value from the router surfaces as a ValueError at the call
# site rather than a CheckViolationError from deep inside asyncpg.
_RESPONSES = frozenset({"accepted", "declined", "ignored"})

# Category -> patterns. The category itself becomes the suppression
# topic_key (R5): a decline on one category doesn't suppress the others.
KEYWORD_TOPICS = {
    "havent_in_a_while": [r"давно мы не", r"давно не (были|ездили|собирались|виделись)"],
    "should_go_somewhere": [
        r"надо бы .*(съездить|сходить|выбраться)",
        r"может.*(выходных|выходные).*(махн|съезд)",
        r"куда-нибудь.*съездить",
    ],
    "lets_go": [r"\bпоехали\b", r"\bмахнём\b", r"\bмахнем\b"],
}

_SUGGESTION_TEXT = "О, хотите, помогу организовать?"

_CLASSIFY_INSTRUCTION = (
    "The message below was flagged by a cheap keyword filter as possibly "
    "about organizing a group event (a trip, outing, or gathering). "
    "Answer true only if it's a genuine, present-tense opening to organize "
    "something together now. Answer false for nostalgia, grief, an "
    "unrelated conversation that happens to share the words, or a heated "
    "argument — anything where a cheerful 'want help organizing?' would "
    "land badly."
)


def match_topic(text: str) -> str | None:
    lowered = text.lower()
    for topic, patterns in KEYWORD_TOPICS.items():
        if any(re.search(p, lowered) for p in patterns):
            return topic
    return None


async def maybe_suggest(pool, telegram_bot, chat_id: int, text: str) -> dict:
    topic_key = match_topic(text)
    if topic_key is None:
        return {"suggested": False, "reason": "no_keyword_match"}

    rate_limited = await pool.fetchval(
        """
        SELECT EXISTS (
            SELECT 1 FROM proactive_suggestions
            WHERE chat_id = $1 AND suggested_at > now() - interval '7 days'
        )
        """,
        chat_id,
    )
    if rate_limited:
        return {"suggested": False, "reason": "rate_limited"}

    suppressed = await pool.fetchval(
        """
        SELECT EXISTS (
            SELECT 1 FROM proactive_suggestions
            WHERE chat_id = $1 AND topic_key = $2
              AND suggested_at > now() - interval '30 days'
              AND response IS DISTINCT FROM 'accepted'
        )
        """,
        chat_id, topic_key,
    )
    if suppressed:
        return {"suggested": False, "reason": "topic_suppressed"}

    is_lead = await classify(_CLASSIFY_INSTRUCTION, text)
    await decision_log.log_decision(
        pool, chat_id=chat_id, user_id=None, raw_text=text, stage="proactive_filter",
        decision={"topic_key": topic_key, "result": "lead" if is_lead else "not_a_lead"},
    )
    if not is_lead:
        return {"suggested": False, "reason": "not_a_lead"}

    # The checks above are advisory — they return the precise reason and, per
    # R5, keep the model call off the rate-limited path. They cannot enforce
    # the limit: two messages arriving together both pass them and both
    # suggest. The claim is taken again under a per-chat advisory lock, which
    # is what actually makes "one suggestion per chat per week" hold.
    async with pool.acquire() as conn:
        async with conn.transaction():
            await conn.execute("SELECT pg_advisory_xact_lock($1)", chat_id)
            claimed = await conn.fetchrow(
                """
                INSERT INTO proactive_suggestions (chat_id, topic_key)
                SELECT $1, $2
                WHERE NOT EXISTS (
                    SELECT 1 FROM proactive_suggestions
                    WHERE chat_id = $1 AND suggested_at > now() - interval '7 days'
                )
                RETURNING id
                """,
                chat_id, topic_key,
            )
    if claimed is None:
        return {"suggested": False, "reason": "rate_limited"}

    try:
        await telegram_bot.send_message(chat_id=chat_id, text=_SUGGESTION_TEXT)
    except Exception:
        # Nobody saw this suggestion, so it must not silence the chat for a
        # week, and must not sit there waiting for get_pending_suggestion to
        # read the next unrelated message as a reply to it. Withdraw the claim.
        # Raising instead would take down S9's handling of an ordinary message
        # in a dormant chat, which R10 forbids.
        log.warning("Proactive suggestion to chat %s could not be sent", chat_id, exc_info=True)
        await pool.execute("DELETE FROM proactive_suggestions WHERE id = $1", claimed["id"])
        return {"suggested": False, "reason": "send_failed"}

    return {"suggested": True, "suggestion_id": claimed["id"]}


async def get_pending_suggestion(pool, chat_id: int):
    return await pool.fetchrow(
        """
        SELECT * FROM proactive_suggestions
        WHERE chat_id = $1 AND response IS NULL AND suggested_at > now() - interval '2 days'
        ORDER BY suggested_at DESC LIMIT 1
        """,
        chat_id,
    )


async def resolve_suggestion(pool, suggestion_id: int, response: str) -> None:
    if response not in _RESPONSES:
        raise ValueError(f"response must be one of {sorted(_RESPONSES)}, got {response!r}")
    await pool.execute(
        "UPDATE proactive_suggestions SET response = $2 WHERE id = $1",
        suggestion_id, response,
    )
