"""Renders the participant roster as plain text, one line per person.

Same reasoning as bot/list_render.py: a fixed, code-owned icon per status
instead of leaving the wording to the model, so the same status always reads
the same way. Each line's icon is a pure function of the current status —
there is no cached rendering anywhere, so re-rendering after a status change
is the entire "update mechanism".
"""

from bot import people

# "maybe" is a hedged reply ("может быть", "постараюсь") — distinct from
# "unknown", which means nobody has answered at all. Without that distinction
# the ❓/◻️ icons the user asked for would have nothing different to point at.
_ICONS = {
    "confirmed": "✅",
    "unknown": "◻️",
    "maybe": "❓",
    "declined": "❌",
}
# A status this module doesn't recognise (a future addition, or a row from
# before some status existed) still needs to render as *something* rather than
# raise — ◻️ ("unclear") is the closest honest reading.
_DEFAULT_ICON = "◻️"

_EMPTY_ROSTER = "Пока никого не записал."


def _participant_line(participant: dict) -> str:
    icon = _ICONS.get(participant.get("status"), _DEFAULT_ICON)
    who = people.mention(participant.get("display_name"), participant.get("username"))
    return f"{icon} {who}"


def render(participants: list[dict]) -> str:
    if not participants:
        return _EMPTY_ROSTER
    return "\n".join(_participant_line(p) for p in participants)
