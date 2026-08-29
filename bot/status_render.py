"""Renders the full "как дела с организацией" status report as one fixed
block of plain text.

Requested live, after two rounds of feedback on the model's own free-composed
version: no emoji, a missing "Напоминания" section, "Место" and "Дата" folded
into one line. The underlying problem was structural, not cosmetic — nothing
in code decided the report's shape, so the model reconstructed it from memory
on every reply and drifted a little differently each time. This module is the
fixed shape; bot.router tells the model to relay it character-for-character,
the same contract bot.list_render and bot.participant_render already have.
"""

import bot.telegram_text as telegram_text

_TITLE = "Вот текущая информация по организации встречи:"
_NO_REMINDERS = "Нет запланированных напоминаний"


# The label on the map link. A word rather than the URL: the coordinates are
# unreadable and the raw link is three lines long on a phone.
MAP_LABEL = "🔗 Map"


def _place_and_date(
    place: str | None, event_date: str | None, place_link: str | None = None
) -> str | None:
    # Both, either, or neither can be known at once — R7's "nothing invented"
    # applies here too: a line for a fact nobody has stated is worse than no
    # line at all.
    lines = []
    if place:
        lines.append(f"📍 Место: {place}")
    if place_link:
        # Its own line, under the name. Beside it the two ran together and
        # the name stopped being readable at a glance, which is the one job
        # this line has.
        lines.append(telegram_text.link(place_link, MAP_LABEL))
    if event_date:
        lines.append(f"📅 Дата: {event_date}")
    return "\n".join(lines) if lines else None


def render_reminders(reminders: list[dict]) -> str:
    if not reminders:
        return _NO_REMINDERS
    return "\n".join(_reminder_line(r) for r in reminders)


def _reminder_line(reminder: dict) -> str:
    line = f"{reminder['message']} — {reminder['next_at']}"
    if reminder.get("repeats_every_minutes"):
        line += f" (каждые {reminder['repeats_every_minutes']} мин до {reminder['repeats_until']})"
    if reminder.get("target") and reminder["target"] != "группа":
        line += f" → {reminder['target']}"
    return line


def render_status(
    *, place: str | None, event_date: str | None,
    participants_rendered: str, list_rendered: str, reminders_rendered: str,
    place_link: str | None = None,
) -> str:
    blocks = [_TITLE]

    place_date = _place_and_date(place, event_date, place_link)
    if place_date:
        blocks.append(place_date)

    blocks.append(f"👥 Участники:\n{participants_rendered}")
    blocks.append(f"🛒 Список покупок / вещей:\n\n{list_rendered}")
    blocks.append(f"⏰ Напоминания:\n{reminders_rendered}")

    return "\n\n".join(blocks)
