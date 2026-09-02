import functools
import json
import logging
import os
from datetime import date as date_type
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

from telegram.error import Forbidden

import bot.item_names as item_names
import bot.maps_links as maps_links
import bot.quantity as quantity
import bot.list_render as list_render
import bot.participant_render as participant_render
import bot.session as session
import bot.timezones as timezones
from bot.list_render import LIST_CATEGORIES

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


# Keys the model has actually invented for "where the event is", observed in
# one session's facts table: destination, event_name. Nothing ever wrote
# "place", which is the key event_status reads — so the 📍 line was blank for
# the whole life of that session while the answer sat right there under
# another name.
#
# Canonicalising here rather than asking the instruction to be more specific,
# for the reason this codebase keeps returning to: a free-form key is a
# guess the model re-makes on every call, and one wrong guess is invisible —
# the write succeeds, and only the report is quietly empty.
_PLACE_KEY = "place"
_PLACE_ALIASES = frozenset({
    "place", "destination", "location", "venue", "spot", "address",
    "event_name", "event_place", "event_location", "meeting_place",
    "место", "локация", "адрес", "куда", "место_встречи", "местовстречи",
})


def canonical_fact_key(key: str) -> str:
    """Fold the model's chosen key onto the canonical one where we have a
    reader that depends on an exact name. Only place has such a reader
    (bot.tools.composed.event_status); every other key stays as written,
    since nothing looks those up by name."""
    if isinstance(key, str) and key.strip().lower().replace(" ", "_") in _PLACE_ALIASES:
        return _PLACE_KEY
    return key


async def remember_fact(pool, session_id, key, value) -> dict:
    stored_key = canonical_fact_key(key)
    if stored_key == _PLACE_KEY:
        # A name is a name. Live, the model recorded the place as
        # "Бен шемен, координаты 31.9460200, 34.9434050" — the same mistake
        # as an item called "2 кг мяса", and it costs the same two things:
        # the report reads like a database row, and the name matches nothing
        # in `places`, so the coordinates that would have made a map link
        # are unreachable.
        value = maps_links.strip_coordinates(value)
    await pool.execute(
        "INSERT INTO facts (session_id, key, value) VALUES ($1, $2, $3)",
        session_id, stored_key, value,
    )
    return {"status": "ok", "key": stored_key}


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

    if action_type == "retopic":
        # Keeps the session and everything hanging off it — list, participants,
        # reminders — and only moves what the event is. Closing and reopening
        # would throw away what the group built by hand.
        updated = await session.retopic(
            pool, confirmation["session_id"], params["activity_type"],
            event_date=_as_date(params.get("event_date")),
            event_date_raw=params.get("event_date"),
        )
        # None means the session closed between the proposal and the "yes".
        return {"status": "executed", "activity_type": updated["activity_type"]} if updated \
            else {"status": "not_found"}

    if action_type == "broadcast_message":
        await bot.send_message(chat_id=confirmation["chat_id"], text=params["text"])
        return {"status": "executed"}

    return {"status": "unknown_action_type"}


def _as_date(iso: str | None):
    """An ISO date, or None for anything else — including the empty string the
    strict-JSON schema makes the model send when it has no date. Parsed here
    rather than trusted, so a malformed value leaves the date alone instead of
    failing a confirmation the group already said yes to."""
    if not iso:
        return None
    try:
        return date_type.fromisoformat(iso)
    except (TypeError, ValueError):
        log.warning("Ignoring unparseable event_date %r", iso)
        return None


def _normalize_category(category) -> str:
    """An unrecognized category — including one the model invents — falls
    back to прочее rather than being rejected: refusing here would cost a
    whole turn to fix a cosmetic detail. The vocabulary itself lives in
    bot.list_render, which also needs it for the shown order."""
    return category if category in LIST_CATEGORIES else list_render.DEFAULT_CATEGORY


def _as_amount(amount):
    """A number, or None. The model is asked for one and does not always send
    one — a bare "штук" or an empty string must leave the item without an
    amount rather than crash the insert or record a zero nobody said."""
    if amount is None or amount == "":
        return None
    try:
        return float(amount)
    except (TypeError, ValueError):
        return None


def _stated(restated, value, unit) -> list[dict]:
    """Whichever of the two inputs the caller used, as one list of pairs."""
    if restated is not None:
        return restated
    if value is None:
        return []
    return [{"amount": value, "unit": quantity.normalize_unit(unit)}]


def _amounts_of(row) -> list[dict]:
    """What an item holds, from whichever column knows.

    amounts is where new writes go; amount/unit is the single-pair shape rows
    written before it still carry. Reading both here keeps the older rows
    working and folds them into the new shape the first time anything touches
    them.
    """
    stored = row["amounts"] if "amounts" in row.keys() else None
    if stored:
        return json.loads(stored) if isinstance(stored, str) else list(stored)
    if row["amount"] is not None:
        return [{"amount": float(row["amount"]), "unit": row["unit"]}]
    return []


async def _rendered_list(pool, session_id) -> str:
    """The list as the group should see it, after a change.

    Returned by list_add so a "добавь X" gets a concrete confirmation
    naming what is now on the list. Asked to add three things, the bot
    previously answered nothing at all: the model had only {"status": "ok"}
    to go on, decided no reply was needed, and the person was left unable to
    tell whether anything had been recorded. A rendered block also reaches
    bot/ai/tool_loop.py's verbatim guard, so it is relayed rather than
    paraphrased.
    """
    return (await list_show(pool, session_id))["rendered"]


async def list_add(pool, session_id, name, amount=None, unit=None, amounts=None, relative=False, category=None) -> dict:
    """Add an item, or report that it is already on the list.

    Idempotent on purpose: two people asking for milk, or a model re-adding an
    item it added a moment ago, must not produce two rows. Telling the caller
    which happened lets it answer honestly instead of confirming an addition
    that did not occur.

    A quantity given for an item that already exists without one is filling
    in information ("возьмите картошки" then "картошки, килограмма два"), so
    it is applied and reported as an update. A quantity given for an item that
    already has one is never applied — overwriting would lose whatever was
    said first — and the existing value comes back so the caller can mention it.
    """
    # Reproduced live: an unaddressed message naming who is coming ("Андрюха
    # и Витька тоже придут") can have the cheap silent-capture classifier
    # confuse the people with things to bring, calling this with a person's
    # own name as the item. A person is tracked via set_participant, which
    # this same message likely also (correctly) triggered — so a name that
    # already matches a participant in this session is refused here rather
    # than trusted, regardless of which caller passed it.
    person = await pool.fetchval(
        "SELECT display_name FROM participants WHERE session_id = $1 AND lower(display_name) = lower($2)",
        session_id, name,
    )
    if person is not None:
        return {"status": "looks_like_a_participant", "detail": person}

    # The amount is already its own column, and the case someone happened to
    # speak in is not part of the item's identity: "2 пачки макарон" is the
    # same thing as "макароны". See bot/item_names.py.
    name = item_names.canonical(name)

    # The unique index still keys on lower(name), which only catches an exact
    # repeat. Word-order and leftover-quantity variants have to be found
    # first, or they insert cleanly as a second row for the same thing.
    # A stated amount is the total unless the caller says otherwise. The
    # default used to be the other way round, with a separate amounts
    # parameter for a restatement, and the model did not reliably reach for
    # it: told "Должно быть 700 г бананов" it called with amount/unit, was
    # refused, and the person could not change the number at all. Both
    # failures are possible, and they are not equal — silently setting an
    # amount when someone meant "ещё" is recoverable by saying the total,
    # while refusing a total is a dead end.
    restated = quantity.as_amounts(amounts) if amounts else None
    value = _as_amount(amount)
    key = item_names.match_key(name)
    rows = await pool.fetch(
        "SELECT id, name, status, amount, unit, amounts FROM list_items WHERE session_id = $1",
        session_id,
    )
    matching = [r for r in rows if item_names.match_key(r["name"]) == key]
    # Prefer a row already under the canonical name. Without this the first
    # match wins, and renaming it below collides with the row that already
    # holds that name — a UniqueViolationError out of list_add, reproduced
    # live on a list holding both "банан" and "бананы" from before matching
    # was by lemma.
    twin = next((r for r in matching if r["name"] == name), None) or (
        matching[0] if matching else None
    )

    if twin is None:
        row = await pool.fetchrow(
            """
            INSERT INTO list_items (session_id, name, amount, unit, amounts, category)
            VALUES ($1, $2, $3, $4, $5, $6)
            ON CONFLICT (session_id, lower(name)) DO NOTHING
            RETURNING id
            """,
            # Keyed off the parsed value, not the raw argument: amount="" is
            # not None but is not a number either, and storing a unit beside a
            # NULL amount leaves a row claiming "килограмм" of nothing.
            session_id, name, value,
            quantity.normalize_unit(unit) if value is not None else None,
            json.dumps(_stated(restated, value, unit)) or None,
            _normalize_category(category),
        )
        if row is not None:
            return {"status": "ok", "item_id": row["id"],
                    "rendered": await _rendered_list(pool, session_id)}

    if twin is not None:
        # Everything else that matched is the same item under another
        # spelling — rows written before names were canonicalised, or before
        # matching was by lemma. Fold them into the twin rather than leaving
        # the list showing one thing twice, carrying over an amount or a
        # claim the surviving row does not have so merging never loses what
        # someone said.
        for other in matching:
            if other["id"] == twin["id"]:
                continue
            await pool.execute(
                """
                UPDATE list_items AS keep SET
                    amount = COALESCE(keep.amount, drop_row.amount),
                    unit = COALESCE(keep.unit, drop_row.unit),
                    quantity = COALESCE(keep.quantity, drop_row.quantity),
                    claimed_by = COALESCE(keep.claimed_by, drop_row.claimed_by),
                    claimed_by_user_id = COALESCE(keep.claimed_by_user_id,
                                                  drop_row.claimed_by_user_id)
                FROM list_items AS drop_row
                WHERE keep.id = $1 AND drop_row.id = $2
                """,
                twin["id"], other["id"],
            )
            await pool.execute("DELETE FROM list_items WHERE id = $1", other["id"])

        if twin["name"] != name and twin["name"] != item_names.canonical(twin["name"]):
            # The surviving row carries a name that is not its own canonical
            # form — one live list had "одна бутылка чай", which matches
            # "чай" by key but reads as nonsense. A name that is already
            # canonical is left alone: matching is by lemma, so "банан"
            # matches a stored "бананы", and renaming on that basis rewrote
            # a group's plural into the model's singular. Repair is for
            # spellings nothing would ever produce today, not for a
            # different but perfectly good way of saying the same thing.
            await pool.execute(
                "UPDATE list_items SET name = $2 WHERE id = $1", twin["id"], name
            )
        twin = await pool.fetchrow(
            "SELECT id, name, status, amount, unit, amounts FROM list_items WHERE id = $1", twin["id"]
        )

    existing = twin or await pool.fetchrow(
        "SELECT id, status, amount, unit, amounts FROM list_items "
        "WHERE session_id = $1 AND lower(name) = lower($2)",
        session_id, name,
    )
    before = _amounts_of(existing)
    was = quantity.combine(before)
    after = restated if restated is not None else _stated(None, value, unit)

    # A restatement, or a plain stated total, replaces what is there.
    if after and not relative and quantity.combine(after) != was:
        return await _replace_amount(pool, session_id, existing, after, was, name)

    # Filling in a blank is stating information, so a first amount applies
    # even when they said "ещё" — there was nothing to add to.
    if after and not before:
        return await _replace_amount(pool, session_id, existing, after, was, name)

    # A relative change to something that already has an amount, in either
    # direction. This tool does no arithmetic on amounts, by design: "добавь
    # ещё бутылку" against "1 бут." could mean two bottles or a bottle more,
    # and "убери один" from "2 шт." is only obvious until the units differ.
    # Say what is on the list and ask for the whole new value.
    #
    # Both directions land here on purpose. The flag used to be called
    # "adding" and covered only more, so nothing described removal at all —
    # asked "Хлеб. Убери один" the model found no action that fit, read the
    # list, and answered with the status report instead.
    return {
        "status": "already_present",
        "item_id": existing["id"],
        "item_status": existing["status"],
        "quantity": was,
        "ask_user": (
            "This is already on the list"
            + (f" as {was}" if was else " with no amount recorded")
            + ". Amounts are never added to or subtracted from here, in either "
            "direction — tell them what is there, say you can neither add nor take "
            "away, and ask what the amount should be in total. Ask it as an "
            "open question and never offer numbers to choose from. Their "
            # This instruction used to end with sample amounts, and the model
            # read them as a menu to hand the person — see the comment in
            # bot/router.py's active-mode instruction for the reply it
            # produced. The examples are gone rather than negated: a prompt
            # cannot show the wrong output and expect it not to be copied.
            "answer will be a plain amount, so pass it without relative."
        ),
        "rendered": await _rendered_list(pool, session_id),
    }


async def _replace_amount(pool, session_id, existing, after, was, name=None) -> dict:
    """Store a stated amount, replacing whatever was there."""
    await pool.execute(
        "UPDATE list_items SET amounts = $2, amount = NULL, unit = NULL WHERE id = $1",
        existing["id"], json.dumps(after),
    )
    now = quantity.combine(after)
    result = {"status": "updated", "item_id": existing["id"], "quantity": now}
    if was is None:
        # Nothing was overwritten — the item had no amount. The list is the
        # answer, as for any other addition.
        result["rendered"] = await _rendered_list(pool, session_id)
        return result

    # Something was overwritten, so the reply has to name both values.
    # Deliberately no "rendered": that field is relayed verbatim by
    # bot/turn_outcome.py, and answering "я поменял" with the whole list
    # hides the one line that changed. Live, "добавь бутылку пива" against
    # "2 бут." wrote 1 бут. and the group was shown eleven unchanged rows.
    result["previous_quantity"] = was
    result["say"] = f"{name or 'Позиция'}: было {was}, стало {now}."
    result["ask_user"] = (
        f"This replaced an amount that was already recorded ({was} -> {now}). "
        "Say both the old value and the new one, so a wrong replacement is "
        "visible and can be corrected — never answer with just the list."
    )
    return result


async def list_show(pool, session_id) -> dict:
    """Return the raw rows (so the model can reason about them) and the
    rendered plain text (what it should actually show — R4)."""
    rows = await pool.fetch(
        """
        SELECT name, status, amount, unit, amounts, quantity, category,
               claimed_by, claimed_by_user_id
        FROM list_items WHERE session_id = $1 ORDER BY created_at
        """,
        session_id,
    )
    items = [
        {
            "name": r["name"], "status": r["status"],
            # One rendered string for both storage shapes. quantity is the
            # free text rows written before amount/unit still carry; it is
            # the fallback, never the preference.
            "quantity": quantity.combine(_amounts_of(r)) or r["quantity"],
            "amounts": _amounts_of(r),
            "category": r["category"], "claimed_by": r["claimed_by"],
            "claimed_by_user_id": r["claimed_by_user_id"],
        }
        for r in rows
    ]
    return {"items": items, "rendered": list_render.render(items)}


async def _resolve_item_name(pool, session_id, name) -> str:
    """The stored name for what the caller means, or the name as given.

    list_add strips quantity words from what it stores, so a later "взял 2 кг
    мяса" would not match the row it created as "мяса" — an exact-name lookup
    would report the item as absent and invite a duplicate. Falls back to the
    same word-set comparison list_add dedupes on, and returns the input
    unchanged when nothing matches so the caller's own not_found handling
    still runs.
    """
    rows = await pool.fetch(
        "SELECT name FROM list_items WHERE session_id = $1", session_id
    )
    for row in rows:
        if row["name"].lower() == (name or "").lower():
            return row["name"]
    key = item_names.match_key(name)
    twin = next((r for r in rows if item_names.match_key(r["name"]) == key), None)
    return twin["name"] if twin else name


async def list_check_off(pool, session_id, name) -> dict:
    name = await _resolve_item_name(pool, session_id, name)
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


async def list_claim(pool, session_id, name, claimed_by, claimed_by_user_id=None) -> dict:
    """Record who is bringing an item, matched by name the same way
    list_check_off is — case-insensitively, with the real names handed back on
    a miss so a model that mismatches a name has something to retry with."""
    name = await _resolve_item_name(pool, session_id, name)
    row = await pool.fetchrow(
        """
        UPDATE list_items SET claimed_by = $3, claimed_by_user_id = $4
        WHERE session_id = $1 AND lower(name) = lower($2)
        RETURNING id
        """,
        session_id, name, claimed_by, claimed_by_user_id,
    )
    if row:
        return {"status": "ok"}
    rows = await pool.fetch("SELECT name FROM list_items WHERE session_id = $1", session_id)
    return {"status": "not_found", "items_on_the_list": [r["name"] for r in rows]}


async def list_unclaim(pool, session_id, name) -> dict:
    """Clear a claim: the item goes back among the unclaimed (R4's third
    criterion), which the sort in bot.list_render turns into moving it back to
    the top of the table."""
    name = await _resolve_item_name(pool, session_id, name)
    row = await pool.fetchrow(
        """
        UPDATE list_items SET claimed_by = NULL, claimed_by_user_id = NULL
        WHERE session_id = $1 AND lower(name) = lower($2)
        RETURNING id
        """,
        session_id, name,
    )
    if row:
        return {"status": "ok"}
    rows = await pool.fetch("SELECT name FROM list_items WHERE session_id = $1", session_id)
    return {"status": "not_found", "items_on_the_list": [r["name"] for r in rows]}


async def list_remove_item(pool, session_id, name) -> dict:
    name = await _resolve_item_name(pool, session_id, name)
    session_row = await _session_row(pool, session_id)
    if session_row is None:
        return {"status": "unknown_session"}
    return await propose_confirmation(
        pool, chat_id=session_row["chat_id"], session_id=session_id,
        action_type="list_remove_item", action_params={"name": name},
    )


async def get_participant_status(pool, session_id, *, user_id) -> str | None:
    """Whether this telegram user is already a known participant, and if so
    what their status is. None means "never recorded" — the caller's cue to
    treat this as a genuinely new join rather than a rejoin.
    """
    return await pool.fetchval(
        "SELECT status FROM participants WHERE session_id = $1 AND user_id = $2",
        session_id, user_id,
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
    """List recorded participants, plus how complete that list is.

    The roster is partial by construction (see EPIC.md's "What the Telegram
    Bot API cannot do" — there is no way to list a group's members), so every
    caller needs both counts to say so rather than presenting a handful of
    names as if that were everyone. chat_member_count is read from the stored
    column rather than calling Telegram here: that keeps an ordinary
    participants question off the network path, and a value at most
    GROUP_SYNC_INTERVAL_SECONDS old is accurate enough for a caveat. It comes
    back None — never 0 — when it has never been fetched, since 0 would read
    as an empty group rather than "unknown".
    """
    session_row = await _session_row(pool, session_id, columns="chat_id")
    rows = await pool.fetch(
        "SELECT user_id, display_name, status FROM participants WHERE session_id = $1 ORDER BY id",
        session_id,
    )
    participants = [
        {"user_id": r["user_id"], "display_name": r["display_name"], "status": r["status"]}
        for r in rows
    ]
    chat_member_count = None
    if session_row is not None:
        chat_member_count = await pool.fetchval(
            "SELECT member_count FROM chats WHERE chat_id = $1", session_row["chat_id"]
        )
    return {
        "participants": participants,
        "chat_member_count": chat_member_count,
        "recorded_count": len(participants),
        "rendered": participant_render.render(participants),
    }


async def nudge_unconfirmed_participants(pool, telegram_bot, session_id) -> dict:
    """DM every participant whose status is still unknown.

    Scoped to 'unknown' only, not 'maybe' — someone who hedged has already
    answered, just non-committally; re-nudging them is chasing a response
    they already gave, not the silence this tool exists to break.

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
    # A reminder addressed to one person is read in that person's wall clock:
    # "напомни Васе в 9" means nine o'clock where Вася is, whatever time it is
    # in the group. Only their own stated zone counts — falling back to the
    # chat's is what happens for everyone else, including the whole group.
    chat_tz = await timezones.chat_timezone(pool, chat_id)
    personal = await timezones.user_timezone(pool, target_user_id)
    if personal is not None:
        known, tz = personal, ZoneInfo(personal)
    else:
        known = await timezones.effective_timezone(pool, chat_id)
        tz = chat_tz
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
    if personal is not None and personal != str(chat_tz):
        # Say which clock this was read in whenever it is not the chat's own.
        # Two people reading "в 9" in the same chat and meaning different
        # instants is exactly the confusion worth spending a sentence on, and
        # a wrong assumption is only correctable if it is visible.
        result["target_timezone"] = personal
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

    chat_tz = await timezones.chat_timezone(pool, session_row["chat_id"])
    rows = await pool.fetch(
        """
        SELECT id, message, remind_at, target_user_id, repeat_every_minutes, repeat_until
        FROM reminders WHERE session_id = $1 AND status = 'pending' ORDER BY remind_at
        """,
        session_id,
    )

    reminders = []
    for r in rows:
        # Rendered in the clock the reminder will actually keep, not the
        # chat's, or the list would quietly misreport every personal one.
        personal = await timezones.user_timezone(pool, r["target_user_id"])
        tz = ZoneInfo(personal) if personal is not None else chat_tz
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
            "timezone": str(tz),
            "timezone_differs": str(tz) != str(chat_tz),
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


async def set_timezone(pool, session_id, timezone_name, current_user_id=None, whose="chat") -> dict:
    """Record a timezone a human just stated — the group's, or their own.

    Outranks the zone guessed from a resolved place: someone saying "мы по
    Москве" knows better than the coordinates of a restaurant they looked up.

    `whose="me"` records the speaker instead of the group ("я в Москве", as
    against "мы в Москве"). That is the only way a personal zone is ever set:
    current_user_id is bound by the router from who actually sent the
    message, never supplied by the model — exactly the reasoning that keeps
    the recipient off send_private_message. Otherwise the model could
    relocate a bystander, and every reminder addressed to them with it.

    Recording a person also moves the chats they created, because their zone
    stands in for those chats when nothing better is known (see
    timezones.effective_timezone). Doing it here rather than leaving it to
    the next reminder is what keeps already-scheduled ones honest.
    """
    session_row = await _session_row(pool, session_id)
    if session_row is None:
        return {"status": "unknown_session"}

    if whose == "me":
        if current_user_id is None:
            return {"status": "unknown_speaker",
                    "detail": "nobody is identifiable as the speaker here — set the chat's zone instead"}
        if not await timezones.set_user_timezone(pool, current_user_id, timezone_name):
            return {"status": "bad_timezone",
                    "detail": f"{timezone_name!r} is not an IANA zone name like 'Europe/Moscow'"}
        moved = await timezones.reanchor_user_reminders(pool, current_user_id, timezone_name)
        for chat_id in await timezones.chats_created_by(pool, current_user_id):
            if await timezones.effective_timezone(pool, chat_id) == timezone_name:
                moved += await timezones.reanchor_pending_reminders(pool, chat_id, timezone_name)
        return {"status": "ok", "timezone": timezone_name, "whose": "me",
                "reminders_corrected": moved}

    if not await timezones.set_chat_timezone(pool, session_row["chat_id"], timezone_name):
        return {"status": "bad_timezone",
                "detail": f"{timezone_name!r} is not an IANA zone name like 'Europe/Moscow'"}
    moved = await timezones.reanchor_pending_reminders(pool, session_row["chat_id"], timezone_name)
    return {"status": "ok", "timezone": timezone_name, "whose": "chat",
            "reminders_corrected": moved}


async def send_private_message(pool, telegram_bot, session_id, current_user_id, text) -> dict:
    """DM the person who asked, never the group (R5).

    current_user_id is bound by the router from message.from_user, never
    supplied by the model — exactly the reasoning that keeps chat_id off
    every other tool's declaration (see 0001's S4/S6): a model-chosen
    recipient could DM anyone in any chat the bot has ever seen. A channel
    post has no from_user, so the router binds None here; that is a routing
    failure rather than a Telegram one, so it is reported as 'failed' without
    attempting a send. pool and session_id are accepted, unused, purely to
    keep this tool's signature the same shape as every other session-bound
    one for build_core_registry/_bind_session_context to wire up uniformly.

    Never raises: a failed DM must not take down the turn that produced it,
    the same discipline nudge_unconfirmed_participants already follows.
    """
    if current_user_id is None:
        return {"status": "failed", "detail": "no telegram user to message"}
    try:
        await telegram_bot.send_message(chat_id=current_user_id, text=text)
        return {"status": "ok"}
    except Forbidden:
        return {"status": "cannot_reach", "detail": "the user has never started a chat with the bot"}
    except Exception as e:
        log.warning("Could not send private message to user %s: %s", current_user_id, e)
        return {"status": "failed", "detail": str(e)}


def build_core_registry(pool, telegram_bot) -> dict:
    return {
        "remember_fact": functools.partial(remember_fact, pool),
        "get_facts": functools.partial(get_facts, pool),
        "list_add": functools.partial(list_add, pool),
        "list_show": functools.partial(list_show, pool),
        "list_check_off": functools.partial(list_check_off, pool),
        "list_claim": functools.partial(list_claim, pool),
        "list_unclaim": functools.partial(list_unclaim, pool),
        "list_remove_item": functools.partial(list_remove_item, pool),
        "set_participant": functools.partial(set_participant, pool),
        "get_participants": functools.partial(get_participants, pool),
        "nudge_unconfirmed_participants": functools.partial(nudge_unconfirmed_participants, pool, telegram_bot),
        "reminder_set": functools.partial(reminder_set, pool),
        "reminder_list": functools.partial(reminder_list, pool),
        "reminder_cancel": functools.partial(reminder_cancel, pool),
        "broadcast_message": functools.partial(broadcast_message, pool),
        "set_timezone": functools.partial(set_timezone, pool),
        "send_private_message": functools.partial(send_private_message, pool, telegram_bot),
    }
