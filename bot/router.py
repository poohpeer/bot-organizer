"""The message router: decides what a dormant or active chat's incoming
message means and drives session lifecycle + the tool-calling loop.

R5 is the whole point of this module's shape: `addressed_to_bot` is checked
first, synchronously, with no model involved, on every path. Nothing below
that gate runs unless a human explicitly spoke to the bot.
"""

import logging
import random
import unicodedata

from bot.logging_setup import truncate
from datetime import date, datetime

from google.genai import types

import bot.decision_log as decision_log
import bot.group_info as group_info
import bot.list_render as list_render
import bot.session as session
import bot.timezones as timezones
import bot.tools.composed as composed_tools
import bot.tools.core as core_tools
import bot.tools.external as external_tools
from bot.addressing import addressed_to_bot, bot_was_added
from bot.ai.classify import classify, extract
from bot.ai.client import AllModelsUnavailable, fallback
from bot.ai.tool_loop import run_tool_loop
from bot.formatting import to_plain_text
from bot.session import SessionAlreadyActiveError, start_session

log = logging.getLogger(__name__)

_FALLBACK_MESSAGE = "Не понял, переформулируй, пожалуйста."

# Shown when every model in the chain is refusing. Deliberately different from
# _FALLBACK_MESSAGE: "переформулируй" invites the user to retype a message that
# was perfectly fine, and they would keep retyping while nothing worked. This
# says the problem is mine and that waiting is the fix. Rotated so a chat that
# hits it several times in a row doesn't get the same line every time.
_AI_UNAVAILABLE_MESSAGES = (
    "Мой искусственный интеллект временно закончился. Остался только "
    "естественный, а он у меня, честно говоря, так себе. Попробуйте попозже.",
    "Все нейросети, которые я знаю, дружно сделали вид, что меня не существует. "
    "Обидно, но переживу. Напишите через несколько минут.",
    "Технически я всё ещё здесь. Интеллектуально — уже нет. Дайте мне немного "
    "времени прийти в себя.",
    "Нейронка прилегла отдохнуть и меня с собой не позвала. Загляните чуть позже, "
    "я к тому времени, надеюсь, снова начну соображать.",
)


def _ai_unavailable_message() -> str:
    return random.choice(_AI_UNAVAILABLE_MESSAGES)


# A chat model cannot reliably emit nothing at all: asked for "an empty
# string" it wrote those two words into the group chat instead. A sentinel is
# something it can actually produce, and the phrasings it reached for before
# are caught alongside it — the model changing its mind about how to say
# "nothing" must not become a message.
_SILENT = "<silent>"
_MEANT_TO_BE_SILENT = frozenset({
    _SILENT, "silent", "empty string", "empty", "(empty string)", "\"\"", "''",
    "пустая строка", "пустая строка.", "empty_string", "none", "null",
})


def _has_visible_text(text: str) -> bool:
    if text.strip().strip(".").lower() in _MEANT_TO_BE_SILENT:
        return False
    return any(
        not ch.isspace() and unicodedata.category(ch) not in {"Cc", "Cf"}
        for ch in text
    )

_GREETING_TEMPLATE = (
    "Привет! Я включаюсь только когда меня зовут — упомяните {mention} или "
    "ответьте на моё сообщение, и я начну отслеживать мероприятие."
)

_ASK_WHAT_TO_TRACK = "Что отслеживаем? Опишите мероприятие, которое нужно организовать."
_ALREADY_TRACKING = "Я уже слежу за мероприятием в этом чате — сначала завершите текущую сессию."
_CONFIRM_START = "Понял, отслеживаю: {activity_type}. Пишите всё, что нужно запомнить."

_START_SCHEMA = types.Schema(
    type=types.Type.OBJECT,
    properties={
        "confident": types.Schema(
            type=types.Type.BOOLEAN,
            description="True only if a concrete event/activity to track was named.",
        ),
        "activity_type": types.Schema(
            type=types.Type.STRING,
            description="Short label for the activity, e.g. 'picnic', 'birthday', 'trip'.",
        ),
        "event_date": types.Schema(
            type=types.Type.STRING,
            description="ISO-8601 date (YYYY-MM-DD) if a date is stated anywhere in the "
            "message or chat title, otherwise omitted.",
        ),
        "event_date_raw": types.Schema(
            type=types.Type.STRING,
            description="The date exactly as written (e.g. '15 сентября'), if any.",
        ),
    },
    required=["confident"],
)

_START_INSTRUCTION = (
    "A human just addressed this bot directly in a group chat with no active "
    "organizing session. Decide whether they are explicitly asking the bot to "
    "start tracking a specific event (a picnic, trip, birthday, etc — anything "
    "with a shared list, participants, or a date). Set confident=true only "
    "when a concrete activity was actually named; a vague message, small "
    "talk, or an unrelated question is confident=false. The event's date may "
    "be stated in the message itself, or only in the chat title context "
    "given below — check both."
)


def _text_of(message) -> str:
    return message.text or message.caption or ""


def _parse_event_date(value) -> date | None:
    # The model is asked for ISO-8601 but isn't guaranteed to comply; a
    # malformed date must not crash session start, it should just be dropped
    # (event_date_raw still carries the human-readable phrasing).
    if not isinstance(value, str):
        return None
    try:
        return date.fromisoformat(value)
    except ValueError:
        return None


def _format_event_date(iso_date: str) -> str:
    parsed = _parse_event_date(iso_date)
    # A malformed date from the classifier is shown as-is rather than dropped:
    # unlike session start (where a bad date just means no date was recorded),
    # here it already passed the "something was found" check, so silently
    # dropping it would make the greeting mention a place/activity but go
    # mute about the date the model just said it saw.
    return f"{parsed:%d.%m.%Y}" if parsed is not None else iso_date


def _greeting_with_info(event: dict, member_count, bot_username: str) -> str:
    parts = [event.get("activity_type") or "мероприятие"]
    if event.get("event_date"):
        parts.append(f"дата — {_format_event_date(event['event_date'])}")
    if event.get("place"):
        parts.append(f"место — {event['place']}")
    # A number from get_chat_member_count, never a roster (R7): the group's
    # members are not being listed, so the sentence must not read as if they
    # were known by name.
    count_clause = f" В группе {member_count} человек." if member_count is not None else ""
    return (
        f"Привет! Вижу: {', '.join(parts)}.{count_clause} Чтобы начать отслеживать, "
        f"упомяните @{bot_username} или ответьте на моё сообщение."
    )


async def handle_bot_added(pool, telegram_bot, chat_member_updated, bot_id, bot_username) -> None:
    """Greet a chat the bot was just added to. Being added is not consent
    (R5): this never starts a session, and a promotion/permission change
    (bot_was_added returning False) must stay silent.

    Reports what the group's own title/description already say (R7) — the
    activity, date and place if stated, plus the member count — without
    starting anything: the greeting describes what can be seen, and asking
    the bot to actually track it is still a separate, explicit step.
    """
    if not bot_was_added(chat_member_updated, bot_id):
        return
    chat = chat_member_updated.chat
    info = await group_info.fetch(telegram_bot, chat.id)
    tz = await timezones.chat_timezone(pool, chat.id)
    event = await group_info.extract_event(
        info.get("title"), info.get("description"), timezones.local_date(tz)
    )

    if any(event.get(k) for k in ("activity_type", "event_date", "place")):
        text = _greeting_with_info(event, info.get("member_count"), bot_username)
    else:
        # Nothing was actually found — the plain greeting as it was before
        # this story, not an empty "Поездка: не указано" scaffold (R7's last
        # criterion: nothing invented).
        text = _GREETING_TEMPLATE.format(mention=f"@{bot_username}")

    await telegram_bot.send_message(chat_id=chat.id, text=text)


async def handle_dormant_message(pool, telegram_bot, message, bot_id, bot_username) -> None:
    if not addressed_to_bot(message, bot_id, bot_username):
        # No model call, no DB write, no reply — R5's gate stops right here.
        return

    chat = message.chat
    text = _text_of(message)
    user_id = message.from_user.id if message.from_user else None

    # sessions.chat_id is a foreign key into chats; nothing upstream is
    # guaranteed to have registered this chat yet, and doing it here — only
    # once the gate has passed — keeps the unaddressed path's "no DB write"
    # contract intact (R5).
    await pool.execute(
        "INSERT INTO chats (chat_id, title) VALUES ($1, $2) "
        "ON CONFLICT (chat_id) DO UPDATE SET title = EXCLUDED.title",
        chat.id, chat.title,
    )

    extracted = await extract(
        _START_INSTRUCTION,
        f"Chat title: {chat.title or '(no title)'}\n\nMessage: {text}",
        _START_SCHEMA,
    )

    if not extracted.get("confident"):
        await telegram_bot.send_message(chat_id=chat.id, text=_ASK_WHAT_TO_TRACK)
        await decision_log.log_decision(
            pool, chat_id=chat.id, user_id=user_id, raw_text=text, stage="session_start",
            decision={"confident": False},
        )
        return

    activity_type = extracted.get("activity_type") or "мероприятие"
    event_date_raw = extracted.get("event_date_raw")
    event_date = _parse_event_date(extracted.get("event_date"))

    try:
        await start_session(
            pool, chat.id, activity_type, event_date=event_date, event_date_raw=event_date_raw,
        )
    except SessionAlreadyActiveError:
        await telegram_bot.send_message(chat_id=chat.id, text=_ALREADY_TRACKING)
        await decision_log.log_decision(
            pool, chat_id=chat.id, user_id=user_id, raw_text=text, stage="session_start",
            decision={"confident": True, "activity_type": activity_type, "already_active": True},
        )
        return

    await telegram_bot.send_message(
        chat_id=chat.id, text=_CONFIRM_START.format(activity_type=activity_type)
    )
    await decision_log.log_decision(
        pool, chat_id=chat.id, user_id=user_id, raw_text=text, stage="session_start",
        decision={
            "confident": True, "activity_type": activity_type,
            "event_date": event_date.isoformat() if event_date else None,
            "event_date_raw": event_date_raw,
        },
    )


# --- Active-session routing --------------------------------------------------

_ALREADY_CLOSED = "Эта сессия уже закрыта."
_CONFIRMED_NOTHING = "Не получилось это сделать — похоже, уже неактуально."
_STILL_ACTIVE_ACK = "Понял, продолжаю следить."
_CONFIRMED_DONE = "Готово."
_CONFIRMED_CANCELLED = "Отменил."

_YES_NO_UNRELATED_SCHEMA = types.Schema(
    type=types.Type.OBJECT,
    properties={"reply": types.Schema(type=types.Type.STRING, enum=["yes", "no", "unrelated"])},
    required=["reply"],
)

_CLOSING_REPLY_INSTRUCTION = (
    "The bot just asked in this group chat: 'How did it go — still need me, "
    "or good to close?'. Classify the message below relative to that "
    "question. 'yes' = it's over, close the session. 'no' = not yet, keep "
    "going. 'unrelated' = this message isn't actually answering that "
    "question at all."
)

_CONFIRMATION_REPLY_INSTRUCTION_TEMPLATE = (
    "The bot proposed a sensitive action and is waiting for confirmation: "
    "action={action_type!r} params={action_params!r}. Classify the message "
    "below. 'yes' = clearly confirms doing it. 'no' = clearly declines or "
    "cancels it. 'unrelated' = this message isn't actually answering that."
)

# There is deliberately no keyword list here (R3): this is the only place
# that decides a human wants the bot gone, and it decides it by asking the
# model, not by matching text.
_STOP_INSTRUCTION = (
    "The bot is actively tracking an event for this group chat and was just "
    "addressed directly. Answer true only if this message is an explicit, "
    "direct instruction telling the bot it is no longer needed / to stop "
    "tracking (e.g. 'that's it, thanks', 'you can rest now', 'we're done', "
    "'больше не нужен'). Chat that merely sounds like the event is wrapping "
    "up, without directly telling the bot to stop, is false. An ordinary "
    "question or request is also false."
)

_SILENT_CAPTURE_SCHEMA = types.Schema(
    type=types.Type.OBJECT,
    properties={
        "list_items": types.Schema(
            type=types.Type.ARRAY, items=types.Schema(type=types.Type.STRING),
            description="Item names to add to the shared list, if this message names "
            "things to get/buy/bring.",
        ),
        "checked_off_items": types.Schema(
            type=types.Type.ARRAY, items=types.Schema(type=types.Type.STRING),
            description="Item names to mark as already obtained, if this message says so.",
        ),
        "facts": types.Schema(
            type=types.Type.ARRAY,
            items=types.Schema(
                type=types.Type.OBJECT,
                properties={
                    "key": types.Schema(type=types.Type.STRING),
                    "value": types.Schema(type=types.Type.STRING),
                },
                required=["key", "value"],
            ),
            description="Any other durable fact worth remembering (destination, "
            "headcount, allergy, who brings what, etc), as key/value pairs.",
        ),
    },
)

_SILENT_CAPTURE_INSTRUCTION = (
    "This message was NOT addressed to the bot — it is ordinary group chat "
    "the bot is quietly listening to during an active organizing session. "
    "Extract only what is worth remembering for later: items to get/buy/"
    "bring (list_items), items already obtained (checked_off_items), and "
    "any other durable fact (facts). Leave a field empty if this message "
    "has nothing for it — never guess or invent."
)

_ACTIVE_MODE_SYSTEM_INSTRUCTION = (
    "You are a group-chat organizing assistant, currently actively tracking "
    "one event. Use the available tools to remember facts, manage the "
    "shared list, track participant confirmations, schedule reminders, and "
    "answer questions using grounded lookups — never invent a fact that "
    "wasn't found by a tool.\n"
    "When someone says they already have, bought or brought something that "
    "belongs on the shared list, call list_check_off for that item. Do not "
    "record it with remember_fact instead: the list is what the group reads, "
    "and a fact nobody looks at leaves the item showing as still needed.\n"
    "Add each item only once. If list_add reports already_present, the item "
    "is on the list — say so rather than adding it again.\n"
    "Keep item names exactly as the group wrote them, in their language. "
    "Never translate or transliterate a name: \"cucumbers\" and \"огурцы\" are "
    "two different items to the list, so translating one turns checking it "
    "off into adding a second copy. If list_check_off reports not_found it "
    "returns the names actually on the list — pick the matching one and call "
    "it again rather than adding anything.\n"
    "If a message only gives you something to record and needs no reply, "
    f"answer with exactly {_SILENT!r} and nothing else — do not narrate what "
    "you just recorded. Never write the words \"empty string\".\n"
    "When someone asks to be reminded repeatedly, ask how long to keep "
    "reminding before scheduling anything — \"следующие 3 часа\", \"до "
    "завтра до 17:00\" — and pass it as repeat_until. If reminder_set "
    "returns repeat_needs_an_end, repeat_too_frequent or "
    "repeat_end_in_the_past, say what is wrong and ask again; do not "
    "schedule a one-off instead without saying so. To answer \"what is "
    "scheduled\", call reminder_list — never say you have no way to check.\n"
    "When someone names an amount, pass it to list_add as quantity, in their "
    "words — \"пару бутылок\", \"кг\". Never invent an amount that was not "
    "said. When someone says they will bring something, call list_claim with "
    "their name; if they say they cannot after all, call list_unclaim. "
    "Categorise each item with one of: " + ", ".join(list_render.LIST_CATEGORIES) + ".\n"
    "When someone asks you to send them something privately — \"пошли мне в "
    "личку\", \"в лс\" — call send_private_message and reply in the group "
    "with one short line saying you have sent it. If it returns "
    "cannot_reach, tell them in the group that they need to open a chat with "
    "you first and press Start; do not repeat the private content in the "
    "group.\n"
    "Always reply in Russian, whatever language the incoming message is in. "
    "Leave proper nouns and list item names exactly as they were written — "
    "\"Ben Shemen\" and \"sparklers\" stay as they are; never transliterate "
    "them."
)

_SESSION_BOUND_TOOLS = frozenset({
    "remember_fact", "get_facts", "list_add", "list_show", "list_check_off",
    "list_claim", "list_unclaim",
    "list_remove_item", "set_participant", "get_participants",
    "nudge_unconfirmed_participants", "reminder_set", "reminder_list", "reminder_cancel",
    "broadcast_message", "set_timezone", "resolve_and_save_place",
    "send_location", "archive_lookup", "send_private_message",
})

# Tools whose recipient must be whoever is actually talking, never a
# model-supplied id (R5) — the same reasoning that keeps session_id off the
# model for _SESSION_BOUND_TOOLS above. Every one of these is also in
# _SESSION_BOUND_TOOLS, since current_user_id is bound in the same wrapper.
_CURRENT_USER_BOUND_TOOLS = frozenset({"send_private_message"})

_SELF_DISPLAY_NAMES = frozenset({
    "i", "me", "myself", "user", "я", "меня", "мне", "сам", "сама",
})


def _display_name_of(user) -> str | None:
    if user is None:
        return None
    full_name = getattr(user, "full_name", None)
    if full_name:
        return full_name
    username = getattr(user, "username", None)
    if username:
        return f"@{username}"
    first_name = getattr(user, "first_name", None)
    last_name = getattr(user, "last_name", None)
    name = " ".join(part for part in (first_name, last_name) if part)
    return name or None


async def _active_mode_instruction(pool, chat_id: int, session_id: int, current_user) -> str:
    user_id = getattr(current_user, "id", None)
    display_name = _display_name_of(current_user)
    # Without this the model has no idea what day it is and dates it from
    # whatever its training data suggests: "напомни через 5 минут" was stored
    # as 2025-07-20, over a year in the past. That only looked like it worked
    # because an overdue reminder fires on the next poll.
    tz = await timezones.chat_timezone(pool, chat_id)
    now = datetime.now(tz)
    user_context = (
        f"Current sender: {display_name} (telegram user_id={user_id}). "
        if user_id is not None and display_name else ""
    )
    return (
        _ACTIVE_MODE_SYSTEM_INSTRUCTION
        + "\nCurrent session_id is "
        + str(session_id)
        + ". Always use this exact session_id for every session-bound tool. "
        + user_context
        + "When the current sender refers to themselves as I/me/я/меня, "
        + "record that actual sender, not a literal name like 'I', 'Я', or 'User'. "
        + f"\nRight now it is {now:%Y-%m-%d %H:%M} ({tz}), a {now:%A}. "
        + "Work out every date and time from that, and pass reminder_set an "
        + "ISO-8601 local time — never a date you assumed from memory."
    )


def _bind_session_context(registry: dict, session_id: int, current_user) -> dict:
    """Trust active-session context from the router, not tool args from the model."""
    user_id = getattr(current_user, "id", None)
    display_name = _display_name_of(current_user)
    bound = {}

    for name, fn in registry.items():
        if name not in _SESSION_BOUND_TOOLS:
            bound[name] = fn
            continue

        async def call(*, _fn=fn, _name=name, **kwargs):
            requested_session_id = kwargs.get("session_id")
            if requested_session_id != session_id:
                log.warning(
                    "Model requested %s with session_id=%r; forcing active session_id=%s",
                    _name, requested_session_id, session_id,
                )
            kwargs["session_id"] = session_id

            if _name in _CURRENT_USER_BOUND_TOOLS:
                # Overwritten unconditionally, exactly like session_id above —
                # the declaration doesn't even expose this parameter to the
                # model, so there is nothing here to compare against, only to
                # supply.
                kwargs["current_user_id"] = user_id

            if _name == "set_participant":
                raw_name = str(kwargs.get("display_name") or "").strip().lower()
                if raw_name in _SELF_DISPLAY_NAMES:
                    if display_name:
                        kwargs["display_name"] = display_name
                    if user_id is not None:
                        kwargs["user_id"] = user_id

            return await _fn(**kwargs)

        bound[name] = call

    return bound


def _build_registry(pool, telegram_bot, *, session_id: int | None = None, current_user=None) -> dict:
    registry = {
        **core_tools.build_core_registry(pool, telegram_bot),
        **external_tools.build_external_registry(),
        **composed_tools.build_composed_registry(pool, telegram_bot),
    }
    if session_id is not None:
        registry = _bind_session_context(registry, session_id, current_user)
    return registry


async def _summarize_session(pool, session_id: int) -> str:
    list_result = await core_tools.list_show(pool, session_id)
    participants_result = await core_tools.get_participants(pool, session_id)
    items = ", ".join(i["name"] for i in list_result["items"]) or "пусто"
    confirmed = [p["display_name"] for p in participants_result["participants"] if p["status"] == "confirmed"]
    return f"Готово, сессию закрываю. Список: {items}. Подтвердили: {', '.join(confirmed) or 'никто'}."


async def _silent_capture(pool, telegram_bot, session_id: int, text: str) -> dict:
    """Record whatever a message that wasn't addressed to the bot is worth
    keeping, via the cheap classifier model rather than the full tool loop —
    R1's "adds each item without announcing it" must not cost a primary-model
    turn for every ordinary line of group chat."""
    extracted = await extract(_SILENT_CAPTURE_INSTRUCTION, text, _SILENT_CAPTURE_SCHEMA)
    registry = core_tools.build_core_registry(pool, telegram_bot)
    for name in extracted.get("list_items") or []:
        await registry["list_add"](session_id=session_id, name=name)
    for name in extracted.get("checked_off_items") or []:
        await registry["list_check_off"](session_id=session_id, name=name)
    for fact in extracted.get("facts") or []:
        key, value = fact.get("key"), fact.get("value")
        if key and value:
            await registry["remember_fact"](session_id=session_id, key=key, value=value)
    return extracted


async def handle_active_message(pool, telegram_bot, active_session, message, bot_id, bot_username) -> None:
    session_id, chat_id = active_session["id"], active_session["chat_id"]
    text = _text_of(message)
    user_id = message.from_user.id if message.from_user else None

    if not addressed_to_bot(message, bot_id, bot_username):
        # Listening is not speaking (R5 + R1): record whatever is worth
        # keeping and touch activity, but never send_message on this path.
        log.debug("session %s: not addressed, silent capture only", session_id)
        extracted = await _silent_capture(pool, telegram_bot, session_id, text)
        if any(extracted.get(k) for k in ("list_items", "checked_off_items", "facts")):
            log.info("session %s: captured %s", session_id, truncate(extracted, 200))
        await session.touch_activity(pool, session_id)
        await decision_log.log_decision(
            pool, chat_id=chat_id, user_id=user_id, raw_text=text, stage="silent_capture",
            decision=extracted,
        )
        return

    if active_session["closing_question_asked_at"] is not None:
        reply = (await extract(_CLOSING_REPLY_INSTRUCTION, text, _YES_NO_UNRELATED_SCHEMA)).get("reply")
        if reply == "yes":
            summary = await _summarize_session(pool, session_id)
            # False means the session had already closed — the worker's
            # auto-close beat a late reply. Say so rather than posting a
            # summary that implies this person closed it.
            applied = await session.record_closing_reply(pool, session_id, continued=False)
            await telegram_bot.send_message(
                chat_id=chat_id, text=summary if applied else _ALREADY_CLOSED
            )
            await decision_log.log_decision(
                pool, chat_id=chat_id, user_id=user_id, raw_text=text, stage="closing_reply",
                decision={"reply": "yes", "session_id": session_id},
            )
            return
        if reply == "no":
            applied = await session.record_closing_reply(pool, session_id, continued=True)
            await telegram_bot.send_message(
                chat_id=chat_id, text=_STILL_ACTIVE_ACK if applied else _ALREADY_CLOSED
            )
            await decision_log.log_decision(
                pool, chat_id=chat_id, user_id=user_id, raw_text=text, stage="closing_reply",
                decision={"reply": "no", "session_id": session_id},
            )
            return
        # "unrelated" -> this wasn't actually an answer, fall through below.

    pending_confirmation = await core_tools.get_pending_confirmation(pool, chat_id)
    if pending_confirmation is not None:
        instruction = _CONFIRMATION_REPLY_INSTRUCTION_TEMPLATE.format(
            action_type=pending_confirmation["action_type"],
            action_params=dict(pending_confirmation["action_params"]),
        )
        reply = (await extract(instruction, text, _YES_NO_UNRELATED_SCHEMA)).get("reply")
        if reply == "yes":
            resolved = await core_tools.resolve_confirmation(
                pool, pending_confirmation["id"], confirmed=True
            )
            result = await core_tools.execute_confirmed_action(pool, telegram_bot, resolved)
            # resolve_confirmation returns None when the row was already
            # resolved, and the action can legitimately find nothing to do.
            # Reporting "готово" either way tells the group something happened
            # when it did not — the confident-but-wrong answer R10 forbids.
            await telegram_bot.send_message(
                chat_id=chat_id,
                text=_CONFIRMED_DONE if result.get("status") == "executed" else _CONFIRMED_NOTHING,
            )
            await decision_log.log_decision(
                pool, chat_id=chat_id, user_id=user_id, raw_text=text, stage="confirmation_reply",
                decision={"reply": "yes", "confirmation_id": pending_confirmation["id"], "result": result},
            )
            return
        if reply == "no":
            await core_tools.resolve_confirmation(pool, pending_confirmation["id"], confirmed=False)
            await telegram_bot.send_message(chat_id=chat_id, text=_CONFIRMED_CANCELLED)
            await decision_log.log_decision(
                pool, chat_id=chat_id, user_id=user_id, raw_text=text, stage="confirmation_reply",
                decision={"reply": "no", "confirmation_id": pending_confirmation["id"]},
            )
            return
        # "unrelated" -> fall through to normal processing below.

    log.debug("session %s: addressed, deciding intent", session_id)
    if await classify(_STOP_INSTRUCTION, text):
        # close_session returns False when someone (or the worker's auto-close)
        # got there first. Posting the summary regardless means two people
        # saying "спасибо, всё" at once get two closing summaries.
        summary = await _summarize_session(pool, session_id)
        if not await session.close_session(pool, session_id, reason="explicit_stop"):
            await decision_log.log_decision(
                pool, chat_id=chat_id, user_id=user_id, raw_text=text, stage="session_stop",
                decision={"trigger": "explicit", "session_id": session_id, "result": "already_closed"},
            )
            return
        await telegram_bot.send_message(chat_id=chat_id, text=summary)
        await decision_log.log_decision(
            pool, chat_id=chat_id, user_id=user_id, raw_text=text, stage="session_stop",
            decision={"trigger": "explicit", "session_id": session_id},
        )
        return

    await session.touch_activity(pool, session_id)
    registry = _build_registry(pool, telegram_bot, session_id=session_id, current_user=message.from_user)
    try:
        reply_text = await run_tool_loop(
            fallback, text, registry,
            system_instruction=await _active_mode_instruction(
                pool, chat_id, session_id, message.from_user
            ),
        )
    except AllModelsUnavailable:
        # Every provider is rate-limited or down. Telling the user to
        # rephrase would be a lie and would have them retyping a fine message
        # into a bot that cannot answer any of them.
        log.warning("Every model refused for chat_id=%s session_id=%s", chat_id, session_id)
        reply_text = _ai_unavailable_message()
    except Exception:
        log.exception("Tool loop failed for chat_id=%s session_id=%s", chat_id, session_id)
        reply_text = _FALLBACK_MESSAGE

    # Converted before the silence check below: a model that answers with
    # "**<silent>**" instead of the bare sentinel must still be silenced, and
    # _has_visible_text compares against bare strings. The fixed fallback
    # strings have no Markdown in them, so running them through this too is
    # harmless and keeps one path instead of two.
    reply_text = to_plain_text(reply_text)

    await decision_log.log_decision(
        pool, chat_id=chat_id, user_id=user_id, raw_text=text, stage="tool_call",
        decision={"session_id": session_id, "reply": reply_text},
    )
    if _has_visible_text(reply_text):
        await telegram_bot.send_message(chat_id=chat_id, text=reply_text)


_DM_REPLY_INSTRUCTION = (
    "A group member was privately asked whether they are coming to a planned "
    "outing. Read their reply and answer 'yes' if they are coming, 'no' if "
    "they are not, and 'unrelated' if the message is not an answer to that "
    "question at all."
)


async def handle_private_message(pool, telegram_bot, message) -> bool:
    """Record a participant's answer to a private nudge (R2).

    A DM has no session of its own, so this looks the sender up among the
    participants still marked unknown in any active session. Returns False when
    the message isn't an answer to a pending nudge, so the caller can fall
    through to ordinary handling.
    """
    user_id = message.from_user.id if message.from_user else None
    if user_id is None:
        return False

    pending = await pool.fetchrow(
        """
        SELECT p.id, p.session_id, p.display_name, s.chat_id
        FROM participants p JOIN sessions s ON s.id = p.session_id
        WHERE p.user_id = $1 AND p.status = 'unknown' AND s.status = 'active'
        ORDER BY p.created_at DESC LIMIT 1
        """,
        user_id,
    )
    if pending is None:
        return False

    text = _text_of(message)
    reply = (await extract(_DM_REPLY_INSTRUCTION, text, _YES_NO_UNRELATED_SCHEMA)).get("reply")
    if reply not in ("yes", "no"):
        return False

    status = "confirmed" if reply == "yes" else "declined"
    await core_tools.set_participant(
        pool, pending["session_id"], pending["display_name"], status, user_id=user_id
    )
    await telegram_bot.send_message(
        chat_id=user_id,
        text="Записал, спасибо!" if reply == "yes" else "Понял, передам.",
    )
    await decision_log.log_decision(
        pool, chat_id=pending["chat_id"], user_id=user_id, raw_text=text,
        stage="participant_dm_reply", decision={"status": status, "session_id": pending["session_id"]},
    )
    return True
