"""The message router: decides what a dormant or active chat's incoming
message means and drives session lifecycle + the tool-calling loop.

R5 is the whole point of this module's shape: `addressed_to_bot` is checked
first, synchronously, with no model involved, on every path. Nothing below
that gate runs unless a human explicitly spoke to the bot.
"""

import logging
from datetime import date

from google.genai import types

import bot.decision_log as decision_log
from bot.addressing import addressed_to_bot, bot_was_added
from bot.ai.classify import extract
from bot.session import SessionAlreadyActiveError, start_session

log = logging.getLogger(__name__)

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


async def handle_bot_added(pool, telegram_bot, chat_member_updated, bot_id, bot_username) -> None:
    """Greet a chat the bot was just added to. Being added is not consent
    (R5): this never starts a session, and a promotion/permission change
    (bot_was_added returning False) must stay silent."""
    if not bot_was_added(chat_member_updated, bot_id):
        return
    await telegram_bot.send_message(
        chat_id=chat_member_updated.chat.id,
        text=_GREETING_TEMPLATE.format(mention=f"@{bot_username}"),
    )


async def handle_dormant_message(pool, telegram_bot, message, bot_id, bot_username) -> None:
    if not addressed_to_bot(message, bot_id, bot_username):
        # No model call, no DB write, no reply — R5's gate stops right here.
        return

    chat = message.chat
    text = _text_of(message)
    user_id = message.from_user.id if message.from_user else None

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
