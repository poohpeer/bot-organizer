import logging

import bot.roll_call as roll_call
import bot.session as session
import bot.tools.core as core_tools

log = logging.getLogger(__name__)


async def roster_state(pool, session_id: int) -> dict:
    """Who has answered, who hasn't, and how many people the bot has never
    heard of — the three things every round and the summary are built from.

    Shared with bot/tools/core.py, which sends the first round.
    """
    roster = await core_tools.get_participants(pool, session_id)
    answered, unanswered = roll_call.split_by_answer(roster["participants"])
    return {
        "answered": answered,
        "unanswered": unanswered,
        "others": roll_call.unknown_others(
            roster["chat_member_count"], roster["recorded_count"]
        ),
    }


async def run_roll_calls(pool, telegram_bot) -> dict:
    """Advance every roll call whose next round is due.

    Claimed atomically before sending, but a failed send is put back: a round
    marked as delivered when nothing was posted would silently spend one of
    the three rounds the group was promised.
    """
    asked, summarised = [], []
    for row in await session.claim_due_roll_calls(pool, roll_call.ROUND_INTERVALS_MINUTES):
        state = await roster_state(pool, row["session_id"])
        try:
            outcome = await roll_call.send_round(
                telegram_bot, row["chat_id"],
                answered=state["answered"], unanswered=state["unanswered"],
                others=state["others"], rounds_done=row["prev_rounds_done"],
            )
        except Exception:
            log.warning("Could not post roll call %s in chat %s", row["id"], row["chat_id"],
                        exc_info=True)
            await session.release_roll_call_claim(
                pool, row["id"], row["prev_rounds_done"], row["prev_next_at"]
            )
            continue
        if outcome == "summarised":
            await session.finish_roll_call(pool, row["id"])
            summarised.append(row["id"])
        else:
            asked.append(row["id"])
    return {"asked": asked, "summarised": summarised}
