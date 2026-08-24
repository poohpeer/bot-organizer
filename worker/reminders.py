from datetime import datetime, timedelta, timezone


async def _due_reminders(pool):
    return await pool.fetch(
        "SELECT * FROM reminders WHERE status = 'pending' AND remind_at <= now() ORDER BY remind_at"
    )


async def _last_sent_at(pool, *, target_user_id, chat_id):
    if target_user_id is not None:
        return await pool.fetchval(
            "SELECT MAX(sent_at) FROM reminders WHERE target_user_id = $1 AND status = 'sent'",
            target_user_id,
        )
    return await pool.fetchval(
        "SELECT MAX(sent_at) FROM reminders WHERE target_user_id IS NULL AND chat_id = $1 AND status = 'sent'",
        chat_id,
    )


async def deliver_due_reminders(pool, telegram_bot, *, min_interval_hours: float) -> dict:
    delivered, deferred = [], []
    for r in await _due_reminders(pool):
        last_sent = await _last_sent_at(pool, target_user_id=r["target_user_id"], chat_id=r["chat_id"])
        if last_sent is not None and (datetime.now(timezone.utc) - last_sent) < timedelta(hours=min_interval_hours):
            deferred.append(r["id"])
            continue

        target_chat_id = r["target_user_id"] if r["target_user_id"] is not None else r["chat_id"]
        await telegram_bot.send_message(chat_id=target_chat_id, text=r["message"])
        await pool.execute("UPDATE reminders SET status = 'sent', sent_at = now() WHERE id = $1", r["id"])
        delivered.append(r["id"])

    return {"delivered": delivered, "deferred": deferred}
