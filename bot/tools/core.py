import functools
import logging
import os
from datetime import datetime, timezone

import bot.timezones as timezones

log = logging.getLogger(__name__)

# The floor beneath REMINDER_MIN_INTERVAL_HOURS's per-target rate limit, which
# repeating reminders are exempt from (see worker/reminders.py). Someone asked
# out loud for a cadence still shouldn't be able to spam the chat every few
# seconds.
REMINDER_MIN_REPEAT_MINUTES = int(os.environ.get("REMINDER_MIN_REPEAT_MINUTES", "5"))

# Actions the model is never allowed to perform directly (R10). Each is
# proposed as a pending_confirmation and only executed after an explicit human
# "yes" in chat.
_CONFIRMATION_PROMPTS = {
    "list_remove_item": "delete {name!r} from the list",
    "broadcast_message": "send this to the whole chat: {text!r}",
}


def _local_iso(when: datetime, tz) -> str:
    """Render a stored UTC instant as a naive local ISO string.

    Naive, not offset-suffixed: this is the same shape reminder_set already
    accepts for remind_at/repeat_until, so a time this function renders can be
    fed straight back into another tool call.
    """
    return when.astimezone(tz).replace(tzinfo=None).isoformat()


async def _session_row(pool, session_id, columns="chat_id"):
    """Fetch a session, or None. Tools take session_id from the model, so a
    hallucinated or stale id must produce a usable result rather than a
    TypeError on None subscripting."""
    return await pool.fetchrow(f"SELECT {columns} FROM sessions WHERE id = $1", session_id)


async def remember_fact(pool, session_id, key, value) -> dict:
    await pool.execute(
        "INSERT INTO facts (session_id, key, value) VALUES ($1, $2, $3)",
        session_id, key, value,
    )
    return {"status": "ok"}


async def get_facts(pool, session_id, key=None) -> dict:
    rows = await pool.fetch(
        """
        SELECT DISTINCT ON (key) key, value FROM facts
        WHERE session_id = $1 AND ($2::text IS NULL OR key = $2)
        ORDER BY key, created_at DESC
        """,
        session_id, key,
    )
    return {"facts": {r["key"]: r["value"] for r in rows}}


async def propose_confirmation(pool, *, chat_id, session_id, action_type, action_params) -> dict:
    row = await pool.fetchrow(
        """
        INSERT INTO pending_confirmations (chat_id, session_id, action_type, action_params)
        VALUES ($1, $2, $3, $4) RETURNING id
        """,
        chat_id, session_id, action_type, action_params,
    )
    return {
        "status": "pending_confirmation",
        "confirmation_id": row["id"],
        # Relayed verbatim into the group chat, so it must read as a sentence
        # rather than a dumped params dict.
        "message_for_user": (
            "Just to confirm — you want me to "
            + _CONFIRMATION_PROMPTS.get(action_type, action_type).format(**action_params)
            + "? Reply yes to go ahead, or no to cancel."
        ),
    }


async def get_pending_confirmation(pool, chat_id):
    return await pool.fetchrow(
        """
        SELECT * FROM pending_confirmations
        WHERE chat_id = $1 AND status = 'pending' AND proposed_at > now() - interval '1 day'
        ORDER BY proposed_at DESC LIMIT 1
        """,
        chat_id,
    )


async def resolve_confirmation(pool, confirmation_id, *, confirmed: bool):
    """Resolve a pending confirmation, returning the row, or None if it was
    already resolved.

    The `status = 'pending'` guard is what stops a gated destructive action
    running twice: two "yes" messages processed concurrently (or one update
    redelivered) would otherwise both resolve the same row and both execute.
    """
    return await pool.fetchrow(
        """
        UPDATE pending_confirmations SET status = $2
        WHERE id = $1 AND status = 'pending' RETURNING *
        """,
        confirmation_id, "confirmed" if confirmed else "rejected",
    )


async def execute_confirmed_action(pool, bot, confirmation) -> dict:
    """Perform an action a human has just confirmed.

    Refuses anything not in the 'confirmed' state, so it can't be replayed
    against an already-executed or rejected row even if a caller passes one in.
    """
    if confirmation is None:
        return {"status": "already_resolved"}
    if confirmation["status"] != "confirmed":
        return {"status": "not_confirmed"}

    action_type = confirmation["action_type"]
    params = confirmation["action_params"]

    if action_type == "list_remove_item":
        # A name is unique per session now, so this matches at most one row.
        # The LIMIT stays as a belt-and-braces guard against an unbounded
        # DELETE if that index is ever dropped.
        deleted = await pool.fetchrow(
            """
            DELETE FROM list_items WHERE id = (
                SELECT id FROM list_items
                WHERE session_id = $1 AND lower(name) = lower($2)
                ORDER BY created_at LIMIT 1
            ) RETURNING name
            """,
            confirmation["session_id"], params["name"],
        )
        # The item may have been renamed or already removed between proposal
        # and confirmation — don't claim a deletion that didn't happen.
        return {"status": "executed"} if deleted else {"status": "not_found"}

    if action_type == "broadcast_message":
        await bot.send_message(chat_id=confirmation["chat_id"], text=params["text"])
        return {"status": "executed"}

    return {"status": "unknown_action_type"}


async def list_add(pool, session_id, name) -> dict:
    """Add an item, or report that it is already on the list.

    Idempotent on purpose: two people asking for milk, or a model re-adding an
    item it added a moment ago, must not produce two rows. Telling the caller
    which happened lets it answer honestly instead of confirming an addition
    that did not occur.
    """
    row = await pool.fetchrow(
        """
        INSERT INTO list_items (session_id, name) VALUES ($1, $2)
        ON CONFLICT (session_id, lower(name)) DO NOTHING
        RETURNING id
        """,
        session_id, name,
    )
    if row is not None:
        return {"status": "ok", "item_id": row["id"]}

    existing = await pool.fetchrow(
        "SELECT id, status FROM list_items WHERE session_id = $1 AND lower(name) = lower($2)",
        session_id, name,
    )
    return {"status": "already_present", "item_id": existing["id"],
            "item_status": existing["status"]}


async def list_show(pool, session_id) -> dict:
    rows = await pool.fetch(
        "SELECT name, status FROM list_items WHERE session_id = $1 ORDER BY created_at",
        session_id,
    )
    return {"items": [{"name": r["name"], "status": r["status"]} for r in rows]}


async def list_check_off(pool, session_id, name) -> dict:
    row = await pool.fetchrow(
        """
        UPDATE list_items SET status = 'checked', checked_at = now()
        WHERE session_id = $1 AND lower(name) = lower($2) AND status = 'pending'
        RETURNING id
        """,
        session_id, name,
    )
    if row:
        return {"status": "ok"}
    # Distinguish "already done" from "never on the list" — two people both
    # saying "I got the cucumbers" shouldn't be told cucumbers aren't listed.
    existing = await pool.fetchrow(
        "SELECT status FROM list_items WHERE session_id = $1 AND lower(name) = lower($2)",
        session_id, name,
    )
    if existing is not None:
        return {"status": "already_checked"}

    # Hand back what is actually on the list. Observed live: a model asked to
    # check off "огурцы" instead added and checked off "cucumbers", having
    # translated the name. A bare not_found gives it nothing to correct with;
    # the real names let it retry against one of them.
    rows = await pool.fetch(
        "SELECT name FROM list_items WHERE session_id = $1 AND status = 'pending'", session_id
    )
    return {"status": "not_found", "items_on_the_list": [r["name"] for r in rows]}


async def list_remove_item(pool, session_id, name) -> dict:
    session_row = await _session_row(pool, session_id)
    if session_row is None:
        return {"status": "unknown_session"}
    return await propose_confirmation(
        pool, chat_id=session_row["chat_id"], session_id=session_id,
        action_type="list_remove_item", action_params={"name": name},
    )


async def set_participant(pool, session_id, display_name, status, user_id=None) -> dict:
    """Record or update one participant's status.

    Identity is deliberately fuzzy: someone is first mentioned by name in the
    group ("Masha is coming"), and only later replies in DM where a real
    telegram user_id is known. Matching on user_id alone would create a second
    row for the same person — get_participants would then report Masha twice
    with conflicting statuses, and the nudge would DM someone who had already
    confirmed. So: match on user_id first, fall back to the name, and backfill
    the user_id onto the existing row once it becomes known.
    """
    row = None
    if user_id is not None:
        row = await pool.fetchrow(
            "SELECT id FROM participants WHERE session_id = $1 AND user_id = $2",
            session_id, user_id,
        )
    if row is None:
        row = await pool.fetchrow(
            """
            SELECT id FROM participants
            WHERE session_id = $1 AND lower(display_name) = lower($2)
              AND (user_id IS NULL OR user_id = $3::bigint)
            ORDER BY (user_id IS NULL) LIMIT 1
            """,
            session_id, display_name, user_id,
        )

    if row is None:
        await pool.execute(
            """
            INSERT INTO participants (session_id, user_id, display_name, status, responded_at)
            VALUES ($1, $2, $3, $4, now())
            """,
            session_id, user_id, display_name, status,
        )
        return {"status": "ok"}

    await pool.execute(
        """
        UPDATE participants SET
            status = $2,
            display_name = $3,
            user_id = COALESCE($4::bigint, user_id),
            responded_at = now()
        WHERE id = $1
        """,
        row["id"], status, display_name, user_id,
    )
    return {"status": "ok"}


async def get_participants(pool, session_id) -> dict:
    rows = await pool.fetch(
        "SELECT user_id, display_name, status FROM participants WHERE session_id = $1",
        session_id,
    )
    return {"participants": [
        {"user_id": r["user_id"], "display_name": r["display_name"], "status": r["status"]}
        for r in rows
    ]}


async def nudge_unconfirmed_participants(pool, telegram_bot, session_id) -> dict:
    """DM every participant whose status is still unknown.

    Each send is isolated: Telegram refuses (Forbidden) to DM anyone who has
    never started a private chat with the bot, which is the normal state for
    most group members. Letting that propagate would abort the run, lose the
    record of who was already reached, and re-DM them all on a retry. Instead
    every failure is collected so the model can tell the group who it could
    not reach.
    """
    session_row = await _session_row(pool, session_id, columns="activity_type")
    if session_row is None:
        return {"status": "unknown_session"}

    rows = await pool.fetch(
        "SELECT user_id, display_name FROM participants WHERE session_id = $1 AND status = 'unknown'",
        session_id,
    )
    nudged, skipped, failed = [], [], []
    for r in rows:
        if r["user_id"] is None:
            skipped.append(r["display_name"])
            continue
        try:
            await telegram_bot.send_message(
                chat_id=r["user_id"],
                text=f"Hey {r['display_name']}, are you in for the {session_row['activity_type']}?",
            )
            nudged.append(r["display_name"])
        except Exception as e:
            log.warning("Could not DM participant %s: %s", r["display_name"], e)
            failed.append(r["display_name"])
    return {"nudged": nudged, "skipped_no_user_id": skipped, "failed_to_reach": failed}


async def reminder_set(pool, session_id, message, remind_at, target_user_id=None,
                        repeat_every_minutes=None, repeat_until=None) -> dict:
    """Queue a reminder for later delivery by the worker.

    chat_id is derived from the session rather than taken from the model: a
    hallucinated chat_id would post this chat's reminder into a different
    group.

    A repeat with no stated end schedules nothing and asks for one instead of
    guessing (R1's second criterion): relying on the system instruction alone
    would make that a suggestion the model is free to skip.
    """
    session_row = await _session_row(pool, session_id)
    if session_row is None:
        return {"status": "unknown_session"}

    try:
        when = datetime.fromisoformat(remind_at)
    except (TypeError, ValueError):
        return {"status": "bad_datetime", "detail": f"could not parse {remind_at!r} as ISO-8601"}
    # The model renders "напомни в 9 утра" as a naive wall-clock time, which
    # means 9am where the group is. Converted here, once, so everything stored
    # is absolute and the worker never deals with zones.
    chat_id = session_row["chat_id"]
    known = await timezones.stored_timezone(pool, chat_id)
    tz = await timezones.chat_timezone(pool, chat_id)
    local_time = when.replace(tzinfo=None) if when.tzinfo is None else None
    when = timezones.to_utc(when, tz)

    repeat_until_utc = None
    if repeat_every_minutes is not None:
        if repeat_until is None:
            return {"status": "repeat_needs_an_end",
                    "detail": "repeat_every_minutes was given without repeat_until — ask how long to keep reminding"}
        if repeat_every_minutes < REMINDER_MIN_REPEAT_MINUTES:
            return {"status": "repeat_too_frequent", "minimum_minutes": REMINDER_MIN_REPEAT_MINUTES}
        try:
            repeat_until_local = datetime.fromisoformat(repeat_until)
        except (TypeError, ValueError):
            return {"status": "bad_datetime", "detail": f"could not parse {repeat_until!r} as ISO-8601"}
        # Same path as remind_at, so a repeat that crosses a DST boundary is
        # anchored to the wall-clock hour that was asked for, not an offset.
        repeat_until_utc = timezones.to_utc(repeat_until_local, tz)
        if repeat_until_utc <= datetime.now(timezone.utc):
            return {"status": "repeat_end_in_the_past"}

    row = await pool.fetchrow(
        """
        INSERT INTO reminders (session_id, chat_id, target_user_id, message, remind_at,
                               local_time, assumed_timezone, repeat_every_minutes, repeat_until)
        VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9) RETURNING id
        """,
        session_id, chat_id, target_user_id, message, when, local_time,
        None if known else str(tz), repeat_every_minutes, repeat_until_utc,
    )
    result = {"status": "ok", "reminder_id": row["id"]}
    if repeat_every_minutes is not None:
        result["repeats_every_minutes"] = repeat_every_minutes
        result["repeats_until"] = _local_iso(repeat_until_utc, tz)
    if known is None and local_time is not None:
        # Nobody has said where this group is, so the time was interpreted in
        # the configured default. Say so instead of silently guessing: the
        # model should name the assumption and offer to correct it, and
        # set_timezone re-anchors this reminder if someone does.
        result["timezone_assumed"] = str(tz)
        result["ask_user"] = (
            f"Timezone unknown for this chat — {remind_at} was read as {tz}. "
            "Tell the user which city or timezone they're in and call set_timezone; "
            "the reminder will be corrected automatically."
        )
    return result


async def reminder_list(pool, session_id) -> dict:
    """List everything currently scheduled for this session (R2).

    Only 'pending' rows: sent and cancelled ones already happened or won't.
    Distinguishing "nothing scheduled" (empty list) from "cannot check" is
    what lets the model say the former plainly instead of claiming the
    latter, which is the complaint in `bugs` this story closes.

    Mirrors list_show in not treating a hallucinated session_id as an error —
    it just has nothing to show.
    """
    session_row = await _session_row(pool, session_id)
    if session_row is None:
        return {"reminders": []}

    tz = await timezones.chat_timezone(pool, session_row["chat_id"])
    rows = await pool.fetch(
        """
        SELECT id, message, remind_at, target_user_id, repeat_every_minutes, repeat_until
        FROM reminders WHERE session_id = $1 AND status = 'pending' ORDER BY remind_at
        """,
        session_id,
    )

    reminders = []
    for r in rows:
        if r["target_user_id"] is None:
            target = "группа"
        else:
            # Fall back to the raw id rather than dropping the row: an
            # unresolved target is still a real, scheduled reminder.
            name = await pool.fetchval(
                "SELECT display_name FROM participants WHERE session_id = $1 AND user_id = $2",
                session_id, r["target_user_id"],
            )
            target = name if name is not None else str(r["target_user_id"])
        reminders.append({
            "reminder_id": r["id"],
            "message": r["message"],
            "next_at": _local_iso(r["remind_at"], tz),
            "repeats_every_minutes": r["repeat_every_minutes"],
            "repeats_until": _local_iso(r["repeat_until"], tz) if r["repeat_until"] is not None else None,
            "target": target,
        })
    return {"reminders": reminders}


async def reminder_cancel(pool, session_id, reminder_id) -> dict:
    """Cancel a pending reminder belonging to this session.

    Scoped by session_id on purpose: reminder_id is a global BIGSERIAL supplied
    by the model, so an unscoped cancel could silently kill another chat's
    reminder.
    """
    result = await pool.execute(
        "UPDATE reminders SET status = 'cancelled' WHERE id = $1 AND session_id = $2 AND status = 'pending'",
        reminder_id, session_id,
    )
    return {"status": "ok"} if result == "UPDATE 1" else {"status": "not_found"}


async def broadcast_message(pool, session_id, text) -> dict:
    """Propose sending an arbitrary message to the whole chat. Gated (R10) —
    this only files a confirmation, it never sends. chat_id comes from the
    session so the gate is always asked of, and the text always lands in, the
    chat the session belongs to."""
    session_row = await _session_row(pool, session_id)
    if session_row is None:
        return {"status": "unknown_session"}
    return await propose_confirmation(
        pool, chat_id=session_row["chat_id"], session_id=session_id,
        action_type="broadcast_message", action_params={"text": text},
    )


async def set_timezone(pool, session_id, timezone_name) -> dict:
    """Record the group's timezone when a human states where they are.

    Outranks the zone guessed from a resolved place: someone saying "мы по
    Москве" knows better than the coordinates of a restaurant they looked up.
    """
    session_row = await _session_row(pool, session_id)
    if session_row is None:
        return {"status": "unknown_session"}
    if not await timezones.set_chat_timezone(pool, session_row["chat_id"], timezone_name):
        return {"status": "bad_timezone",
                "detail": f"{timezone_name!r} is not an IANA zone name like 'Europe/Moscow'"}
    moved = await timezones.reanchor_pending_reminders(pool, session_row["chat_id"], timezone_name)
    return {"status": "ok", "timezone": timezone_name, "reminders_corrected": moved}


def build_core_registry(pool, telegram_bot) -> dict:
    return {
        "remember_fact": functools.partial(remember_fact, pool),
        "get_facts": functools.partial(get_facts, pool),
        "list_add": functools.partial(list_add, pool),
        "list_show": functools.partial(list_show, pool),
        "list_check_off": functools.partial(list_check_off, pool),
        "list_remove_item": functools.partial(list_remove_item, pool),
        "set_participant": functools.partial(set_participant, pool),
        "get_participants": functools.partial(get_participants, pool),
        "nudge_unconfirmed_participants": functools.partial(nudge_unconfirmed_participants, pool, telegram_bot),
        "reminder_set": functools.partial(reminder_set, pool),
        "reminder_list": functools.partial(reminder_list, pool),
        "reminder_cancel": functools.partial(reminder_cancel, pool),
        "broadcast_message": functools.partial(broadcast_message, pool),
        "set_timezone": functools.partial(set_timezone, pool),
    }
