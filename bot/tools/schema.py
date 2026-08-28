from typing import Callable

from google.genai import types

from bot.quantity import UNITS as _QUANTITY_UNITS

Type = types.Type

ToolRegistry = dict[str, Callable]


def _fn(name, description, properties, required):
    return types.FunctionDeclaration(
        name=name,
        description=description,
        parameters=types.Schema(type=Type.OBJECT, properties=properties, required=required),
    )


def _S(t, desc=None):
    return types.Schema(type=t, description=desc)


ALL_TOOLS = types.Tool(function_declarations=[
    _fn("remember_fact", "Store an arbitrary fact about the current session (place, time, who brings what, allergies, anything).",
        {"session_id": _S(Type.INTEGER), "key": _S(Type.STRING), "value": _S(Type.STRING)},
        ["session_id", "key", "value"]),
    _fn("get_facts", "Retrieve previously remembered facts for the session, optionally filtered by key.",
        {"session_id": _S(Type.INTEGER), "key": _S(Type.STRING, "Optional exact key to filter by.")},
        ["session_id"]),
    _fn("list_add", "Add an item to the session's shared list. Split what was said into the thing itself and how much of it: 'два килограмма мяса' is name='мясо', amount=2, unit='килограмм'; 'полкило мяса' is name='мясо', amount=0.5, unit='килограмм'; 'бутылка водки' is name='водка', amount=1, unit='бутылка'; 'бутылка чая' is name='чай', amount=1, unit='бутылка'. A container is always the unit, never part of the name.",
        {"session_id": _S(Type.INTEGER),
         "name": _S(Type.STRING, "The thing itself, without the amount, in the nominative case: 'мясо', not 'мяса' or '2 кг мяса'. Keep the group's own wording and language otherwise."),
         "amount": _S(Type.NUMBER, "How many or how much, as a number. Omit if nobody said — never invent one. Use 1 when a single container was named ('бутылка водки' is amount=1)."),
         "unit": _S(Type.STRING, "One of: " + ", ".join(_QUANTITY_UNITS) + ". Use 'штука' for a plain count. Required whenever amount is given."),
         "relative": _S(Type.BOOLEAN, "True when they asked for MORE or LESS of something rather than saying how much there should be in total. 'добавь ещё бутылку', 'убери один', 'на полкило меньше' are relative=true; 'должно быть 700 г', 'хлеб два', 'хлеба осталось 2' are not. An amount is treated as the total unless this says otherwise; a relative change is refused and the person is asked for the total, because this tool does no arithmetic on amounts."),
         "amounts": types.Schema(
             type=Type.ARRAY,
             description="The whole amount when it takes more than one unit to say — 'ящик и две бутылки' is amounts=[{amount:1,unit:'ящик'},{amount:2,unit:'бутылка'}]. It replaces whatever is stored; for a first or single amount use amount/unit instead.",
             items=types.Schema(
                 type=Type.OBJECT,
                 properties={
                     "amount": _S(Type.NUMBER, "How many or how much."),
                     "unit": _S(Type.STRING, "One of: " + ", ".join(_QUANTITY_UNITS) + "."),
                 },
                 required=["amount", "unit"],
             ),
         ),
         "category": _S(Type.STRING, "One of: мясо, молочка, овощи и фрукты, напитки, хлеб и выпечка, бакалея, посуда, прочее.")},
        ["session_id", "name"]),
    _fn("list_show", "Show the current state of the session's shared list.",
        {"session_id": _S(Type.INTEGER)}, ["session_id"]),
    _fn("list_check_off", "Mark a list item as done/acquired, matched by name.",
        {"session_id": _S(Type.INTEGER), "name": _S(Type.STRING)},
        ["session_id", "name"]),
    _fn("list_claim", "Record who is bringing a list item, matched by name.",
        {"session_id": _S(Type.INTEGER), "name": _S(Type.STRING),
         "claimed_by": _S(Type.STRING, "Name of the person bringing it."),
         "claimed_by_user_id": _S(Type.INTEGER, "Telegram user id, if known.")},
        ["session_id", "name", "claimed_by"]),
    _fn("list_unclaim", "Clear who is bringing a list item, matched by name — call this when someone who claimed an item says they can't after all.",
        {"session_id": _S(Type.INTEGER), "name": _S(Type.STRING)},
        ["session_id", "name"]),
    _fn("list_remove_item", "Permanently delete an item from the list. Destructive — requires human confirmation before it takes effect.",
        {"session_id": _S(Type.INTEGER), "name": _S(Type.STRING)},
        ["session_id", "name"]),
    _fn("set_participant", "Record or update a participant's confirmation status for the session.",
        {"session_id": _S(Type.INTEGER), "display_name": _S(Type.STRING),
         "status": _S(Type.STRING, "One of: unknown, confirmed, maybe, declined. Use 'maybe' for a hedged reply ('может быть', 'постараюсь') — distinct from 'unknown', which means nobody has answered at all."),
         "user_id": _S(Type.INTEGER, "Telegram user id, if known.")},
        ["session_id", "display_name", "status"]),
    _fn("get_participants", "List participants and their confirmation status for the session.",
        {"session_id": _S(Type.INTEGER)}, ["session_id"]),
    _fn("nudge_unconfirmed_participants", "Privately message every participant with unknown status, asking if they're coming. Only call this when a human explicitly asked to check on/chase confirmations.",
        {"session_id": _S(Type.INTEGER)}, ["session_id"]),
    # chat_id is deliberately NOT a parameter on the tools below: it is derived
    # from session_id in code, so a hallucinated chat_id cannot make the bot
    # write into a different group's chat.
    _fn("reminder_set", "Schedule a reminder to be delivered later, to one person or the whole chat. "
        "Pass repeat_every_minutes and repeat_until to make it repeat until that time.",
        {"session_id": _S(Type.INTEGER), "message": _S(Type.STRING),
         "remind_at": _S(Type.STRING, "ISO-8601 datetime."),
         "target_user_id": _S(Type.INTEGER, "Telegram user id, or omit to post in the group chat."),
         "repeat_every_minutes": _S(Type.INTEGER, "Repeat interval in minutes, or omit for a one-off reminder."),
         "repeat_until": _S(Type.STRING, "ISO-8601 datetime the repeats stop at. Required if repeat_every_minutes is given.")},
        ["session_id", "message", "remind_at"]),
    _fn("reminder_list", "List every currently scheduled (pending) reminder for this session, with its next delivery time in the chat's own timezone, whether it repeats, and who it goes to. Call this whenever someone asks what is scheduled.",
        {"session_id": _S(Type.INTEGER)}, ["session_id"]),
    _fn("reminder_cancel", "Cancel a previously scheduled reminder belonging to this session.",
        {"session_id": _S(Type.INTEGER), "reminder_id": _S(Type.INTEGER)},
        ["session_id", "reminder_id"]),
    _fn("broadcast_message", "Send an arbitrary message to the whole chat outside of a normal reply. Destructive — requires human confirmation before it takes effect.",
        {"session_id": _S(Type.INTEGER), "text": _S(Type.STRING)},
        ["session_id", "text"]),
    _fn("set_timezone", "Record which timezone this group is in, so reminders land at the right local time. Call this whenever someone names their city, region or timezone. If reminder_set reports timezone_assumed, ask the group roughly where they are and then call this — already-scheduled reminders are corrected automatically.",
        {"session_id": _S(Type.INTEGER),
         "timezone_name": _S(Type.STRING, "IANA zone name, e.g. 'Europe/Moscow' or 'Asia/Jerusalem'.")},
        ["session_id", "timezone_name"]),
    # session_id and current_user_id are deliberately NOT parameters here: the
    # router binds both from who actually sent the message, the same way
    # chat_id is kept off every other tool's declaration. Exposing the
    # recipient to the model would let it DM anyone in any chat it has seen.
    _fn("send_private_message", "Send a direct message to the person who is currently talking to you in the group, in reply to their request to be answered privately.",
        {"text": _S(Type.STRING)},
        ["text"]),
    _fn("web_search", "Search the web for up-to-date information not already known as a fact.",
        {"query": _S(Type.STRING)}, ["query"]),
    _fn("maps_lookup", "Look up a place by name/description via maps: resolves address, coordinates, and available details/reviews.",
        {"query": _S(Type.STRING)}, ["query"]),
    _fn("weather_lookup", "Get a weather forecast for a coordinate and date.",
        {"lat": _S(Type.NUMBER), "lon": _S(Type.NUMBER), "date": _S(Type.STRING, "ISO-8601 date.")},
        ["lat", "lon", "date"]),
    _fn("resolve_and_save_place", "Resolve a place name to coordinates for this session, reusing a previously saved match instead of re-querying maps if one exists.",
        {"session_id": _S(Type.INTEGER), "place_query": _S(Type.STRING)},
        ["session_id", "place_query"]),
    _fn("send_location", "Send a native map location card for a place already resolved for this session.",
        {"session_id": _S(Type.INTEGER), "place_name": _S(Type.STRING)},
        ["session_id", "place_name"]),
    _fn("archive_lookup", "Look up where this chat has gone before for a given activity type, ranked by recency-weighted frequency, across closed sessions.",
        {"session_id": _S(Type.INTEGER), "activity_type": _S(Type.STRING)},
        ["session_id", "activity_type"]),
    _fn("event_status", "Get the full organizing status report (place, date, participants, shopping list, reminders) as one pre-formatted block of text. Call this whenever someone asks how the organizing is going, what the current status is, or wants a summary — reply with its report field character-for-character, never compose your own version.",
        {"session_id": _S(Type.INTEGER)},
        ["session_id"]),
    _fn("get_chat_info", "Get this group chat's own current title and description, fetched live from Telegram. Call this whenever someone asks what the group/chat is called, or what its title or description says — never say you cannot see it.",
        {"session_id": _S(Type.INTEGER)},
        ["session_id"]),
    _fn("sync_chat_info", "Read the group's title/description live and actually record the place and/or date they state, in one call. Call this when someone asks you to look at the group's title or description and record/save/remember what it says — never claim you saved something without calling this. Returns found_nothing true if the title/description state nothing.",
        {"session_id": _S(Type.INTEGER)},
        ["session_id"]),
])
