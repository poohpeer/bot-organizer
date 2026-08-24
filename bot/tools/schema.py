from typing import Callable

from google.genai import types

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
    _fn("list_add", "Add an item to the session's shared list.",
        {"session_id": _S(Type.INTEGER), "name": _S(Type.STRING)},
        ["session_id", "name"]),
    _fn("list_show", "Show the current state of the session's shared list.",
        {"session_id": _S(Type.INTEGER)}, ["session_id"]),
    _fn("list_check_off", "Mark a list item as done/acquired, matched by name.",
        {"session_id": _S(Type.INTEGER), "name": _S(Type.STRING)},
        ["session_id", "name"]),
    _fn("list_remove_item", "Permanently delete an item from the list. Destructive — requires human confirmation before it takes effect.",
        {"session_id": _S(Type.INTEGER), "name": _S(Type.STRING)},
        ["session_id", "name"]),
    _fn("set_participant", "Record or update a participant's confirmation status for the session.",
        {"session_id": _S(Type.INTEGER), "display_name": _S(Type.STRING),
         "status": _S(Type.STRING, "One of: unknown, confirmed, declined."),
         "user_id": _S(Type.INTEGER, "Telegram user id, if known.")},
        ["session_id", "display_name", "status"]),
    _fn("get_participants", "List participants and their confirmation status for the session.",
        {"session_id": _S(Type.INTEGER)}, ["session_id"]),
    _fn("nudge_unconfirmed_participants", "Privately message every participant with unknown status, asking if they're coming. Only call this when a human explicitly asked to check on/chase confirmations.",
        {"session_id": _S(Type.INTEGER)}, ["session_id"]),
    # chat_id is deliberately NOT a parameter on the tools below: it is derived
    # from session_id in code, so a hallucinated chat_id cannot make the bot
    # write into a different group's chat.
    _fn("reminder_set", "Schedule a reminder to be delivered later, to one person or the whole chat.",
        {"session_id": _S(Type.INTEGER), "message": _S(Type.STRING),
         "remind_at": _S(Type.STRING, "ISO-8601 datetime."),
         "target_user_id": _S(Type.INTEGER, "Telegram user id, or omit to post in the group chat.")},
        ["session_id", "message", "remind_at"]),
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
])
