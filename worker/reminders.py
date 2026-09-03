import logging
from datetime import datetime, timedelta, timezone

import bot.session as session
import bot.timezones as timezones
from bot.tools.core import REMINDER_MAX_DELIVERIES

log = logging.getLogger(__name__)

# A reminder to someone who has blocked the bot can never succeed. Retrying it
# forever would keep the queue permanently non-empty and re-attempt a doomed
# send every poll, so give up after this many tries.
MAX_ATTEMPTS = 3

# Quiet hours apply to repeats only. A one-off was scheduled for a moment
# somebody named — "напомни в 23:00" means 23:00, and holding it until nine in
# the morning would deliver it after whatever it was about. A repeat has no
# such moment: it fires on a cadence, and skipping the night simply moves the
# next one to the start of the waking window, without spending a delivery,
# because the row is not selected at all while the chat is asleep.
_DUE = f"""
    r.status = 'pending'
    AND r.remind_at <= now()
    AND (r.repeat_every_minutes IS NULL OR ({session.WAKING_HOURS_SQL}))
"""


async def _due_reminders(pool):
    # LEFT JOIN, belt and braces. reminders.chat_id is copied from the
    # session, and sessions.chat_id has a foreign key to chats, so the row is
    # always there today — but an inner join would turn any future gap into
    # reminders that silently never fire, and the timezone expression already
    # falls back to the configured default for a NULL.
    return await pool.fetch(
        f"""
        SELECT r.* FROM reminders r LEFT JOIN chats c USING (chat_id)
        WHERE {_DUE}
        ORDER BY r.remind_at
        """,
        timezones.DEFAULT_TIMEZONE, session.QUIET_UNTIL_HOUR, session.QUIET_FROM_HOUR,
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


async def _advance(pool, reminder_id, next_at) -> None:
    """Move a repeating reminder to its next occurrence after a successful
    send.

    Runs after the claim already flipped the row to 'sent', so no other
    worker's claim query (`WHERE status = 'pending'`) can see it in between —
    putting it back to 'pending' here is safe from the same race _claim
    guards against. attempts resets to 0: a failure on an earlier occurrence
    must not count against a later one, or a reminder that failed twice years
    ago would need only one more failure to stop repeating forever.
    """
    await pool.execute(
        """
        UPDATE reminders
        SET remind_at = $2, status = 'pending', attempts = 0, sent_at = NULL
        WHERE id = $1
        """,
        reminder_id, next_at,
    )


async def _count_delivery(pool, reminder_id) -> int:
    """Record that one occurrence actually went out, and say how many have.

    Counted rather than derived from the clock: repeat_until bounds a series
    in time only, so "каждый час до завтра" is twenty-four messages nobody
    asked for in those words. The cap is on how many are sent, and only a
    counter can express that.
    """
    return await pool.fetchval(
        "UPDATE reminders SET deliveries = deliveries + 1 WHERE id = $1 RETURNING deliveries",
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


def _next_occurrence(scheduled, every_minutes: int):
    """The first occurrence strictly after now, counted from the schedule.

    Two things have to hold at once, and they pull in opposite directions.

    Counting from `scheduled` rather than from `now()` is what stops a late
    tick from dragging the whole series later — over a day of half-hourly
    reminders, a few late ticks become real accumulated lag.

    But advancing by exactly one interval means an overdue series delivers one
    missed occurrence per poll, forever, until it catches up. A worker down for
    an hour turns a five-minute repeat into twelve stale messages; and a
    reminder mis-dated into the past — which is exactly what happened when the
    model guessed 2025-07-20 for "через 5 минут" — would post once a minute for
    thirteen days. Missed occurrences are therefore skipped, not queued: the
    reminder fires once and the schedule jumps to the next future slot.
    """
    interval = timedelta(minutes=every_minutes)
    elapsed = datetime.now(timezone.utc) - scheduled
    periods = max(1, -(-elapsed // interval))  # ceil, at least one
    return scheduled + periods * interval


async def deliver_due_reminders(pool, telegram_bot, *, min_interval_hours: float) -> dict:
    delivered, deferred, failed = [], [], []
    for r in await _due_reminders(pool):
        # A cadence someone explicitly asked for out loud is not the unbidden
        # pestering REMINDER_MIN_INTERVAL_HOURS exists to stop — a half-hourly
        # repeat would otherwise be deferred by it forever. See EPIC.md and
        # S2-reminders.md.
        if r["target_user_id"] is not None and r["repeat_every_minutes"] is None:
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

        if r["repeat_every_minutes"] is not None:
            delivered_so_far = await _count_delivery(pool, r["id"])
            next_at = _next_occurrence(r["remind_at"], r["repeat_every_minutes"])
            # Two independent ends, and the series stops at whichever comes
            # first: the time the group asked for, and the ceiling on how many
            # messages any one request may produce.
            if next_at <= r["repeat_until"] and delivered_so_far < REMINDER_MAX_DELIVERIES:
                await _advance(pool, r["id"], next_at)

        delivered.append(r["id"])

    return {"delivered": delivered, "deferred": deferred, "failed": failed}
