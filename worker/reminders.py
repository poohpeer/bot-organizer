import logging
from datetime import datetime, timedelta, timezone

log = logging.getLogger(__name__)

# A reminder to someone who has blocked the bot can never succeed. Retrying it
# forever would keep the queue permanently non-empty and re-attempt a doomed
# send every poll, so give up after this many tries.
MAX_ATTEMPTS = 3


async def _due_reminders(pool):
    return await pool.fetch(
        "SELECT * FROM reminders WHERE status = 'pending' AND remind_at <= now() ORDER BY remind_at"
    )


async def _last_sent_at(pool, target_user_id):
    """When this person was last sent a reminder, across every chat.

    Only direct messages are spaced: REMINDER_MIN_INTERVAL_HOURS exists to stop
    one person being pestered privately. A group reminder was asked for by the
    group and is tied to a moment ("выезжаем через час"), so delaying it by
    hours would deliver it after the event rather than protecting anyone.
    """
    return await pool.fetchval(
        "SELECT MAX(sent_at) FROM reminders WHERE target_user_id = $1 AND status = 'sent'",
        target_user_id,
    )


async def _claim(pool, reminder_id):
    """Take exclusive ownership of one due reminder.

    The status guard is what stops a second worker — or the same worker after a
    restart mid-poll — delivering the same reminder twice: whoever flips the
    row out of 'pending' first is the only one who sends it.
    """
    return await pool.fetchrow(
        "UPDATE reminders SET status = 'sent', sent_at = now() WHERE id = $1 AND status = 'pending' RETURNING id",
        reminder_id,
    )


async def _release(pool, reminder_id) -> str:
    """Hand a claimed reminder back after a failed send, or retire it.

    Returns the status the row ended up in.
    """
    row = await pool.fetchrow(
        """
        UPDATE reminders
        SET attempts = attempts + 1,
            sent_at = NULL,
            status = CASE WHEN attempts + 1 >= $2 THEN 'failed' ELSE 'pending' END
        WHERE id = $1
        RETURNING status
        """,
        reminder_id, MAX_ATTEMPTS,
    )
    return row["status"]


async def deliver_due_reminders(pool, telegram_bot, *, min_interval_hours: float) -> dict:
    delivered, deferred, failed = [], [], []
    for r in await _due_reminders(pool):
        if r["target_user_id"] is not None:
            last_sent = await _last_sent_at(pool, r["target_user_id"])
            if last_sent is not None and (datetime.now(timezone.utc) - last_sent) < timedelta(hours=min_interval_hours):
                deferred.append(r["id"])
                continue

        if await _claim(pool, r["id"]) is None:
            continue

        target_chat_id = r["target_user_id"] if r["target_user_id"] is not None else r["chat_id"]
        log.debug("reminder %s due, delivering to %s", r["id"], target_chat_id)
        try:
            await telegram_bot.send_message(chat_id=target_chat_id, text=r["message"])
        except Exception:
            # One unreachable recipient must not strand every reminder behind
            # it: Telegram refuses to DM anyone who never started the bot, which
            # is the normal state for most group members.
            log.warning("Could not deliver reminder %s to %s", r["id"], target_chat_id, exc_info=True)
            if await _release(pool, r["id"]) == "failed":
                failed.append(r["id"])
            continue

        delivered.append(r["id"])

    return {"delivered": delivered, "deferred": deferred, "failed": failed}
