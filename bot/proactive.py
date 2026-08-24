import re

import bot.decision_log as decision_log
from bot.ai.classify import classify

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

    row = await pool.fetchrow(
        "INSERT INTO proactive_suggestions (chat_id, topic_key) VALUES ($1, $2) RETURNING id",
        chat_id, topic_key,
    )
    await telegram_bot.send_message(chat_id=chat_id, text=_SUGGESTION_TEXT)
    return {"suggested": True, "suggestion_id": row["id"]}
