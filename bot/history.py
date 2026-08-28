"""Recent conversation, so the bot can be answered.

The tool loop was called with no history at all, which is fine while every
message stands alone and fatal the moment the bot asks a question. Live: told
an amount was already set, it asked for the whole new amount; the person
replied "1 литр"; the bot answered "Что именно 1 литр?" — it had no idea it
had just asked. Every clarification the bot offers is unanswerable without
this.

Read from decision_log rather than a new table: it already records the
message and the reply per chat, with an index on (chat_id, created_at).
"""

import os

# Five exchanges. Enough for a clarification and its answer with room to
# spare, and about 180 tokens on real traffic — measured against one live
# conversation, roughly 5% on top of a turn's system instruction and tools.
CONVERSATION_TURNS = int(os.environ.get("CONVERSATION_TURNS", "5"))

# Older than this and "1 литр" is answering something nobody remembers
# asking. A stale question is worse than none: it makes the bot act on a
# thread the group has moved on from.
CONVERSATION_MAX_AGE_MINUTES = int(os.environ.get("CONVERSATION_MAX_AGE_MINUTES", "30"))


async def recent_turns(pool, chat_id: int) -> list[dict]:
    """The last few exchanges in this chat, oldest first.

    Only addressed turns (stage 'tool_call'): the silent-capture path never
    replies, so those rows would put the group's messages in front of the
    model as if the bot had been part of them.
    """
    rows = await pool.fetch(
        """
        SELECT raw_text, decision FROM decision_log
        WHERE chat_id = $1 AND stage = 'tool_call'
          AND created_at > now() - $2 * interval '1 minute'
        ORDER BY created_at DESC LIMIT $3
        """,
        chat_id, CONVERSATION_MAX_AGE_MINUTES, CONVERSATION_TURNS,
    )

    turns: list[dict] = []
    for row in reversed(rows):
        if row["raw_text"]:
            turns.append({"role": "user", "content": row["raw_text"]})
        reply = (row["decision"] or {}).get("reply")
        # A turn the bot stayed silent on contributes the question but no
        # answer; inventing one would tell the model it said something it
        # did not.
        if reply:
            turns.append({"role": "assistant", "content": reply})
    return turns
