from bot.formatting import to_plain_text
from bot.status_render import render_reminders, render_status


def test_matches_the_exact_requested_shape():
    """The literal scenario from the live report: place and date on separate
    lines, no blank line between a header and its content except before the
    shopping list (which embeds list_render's own two-section output)."""
    result = render_status(
        place="Tel Aviv, sea", event_date="20/11",
        participants_rendered="◻️ Витька\n✅ Андрюха\n◻️ Alex",
        list_rendered=(
            "Ещё не разобрали:\n◻️ Андрюха\n◻️ Витька\n◻️ пиво\n\n"
            "Уже взяли:\n✅ арбуз — Alex"
        ),
        reminders_rendered="Нет запланированных напоминаний",
    )

    assert result == (
        "Вот текущая информация по организации встречи:\n\n"
        "📍 Место: Tel Aviv, sea\n"
        "📅 Дата: 20/11\n\n"
        "👥 Участники:\n"
        "◻️ Витька\n✅ Андрюха\n◻️ Alex\n\n"
        "🛒 Список покупок / вещей:\n\n"
        "Ещё не разобрали:\n◻️ Андрюха\n◻️ Витька\n◻️ пиво\n\n"
        "Уже взяли:\n✅ арбуз — Alex\n\n"
        "⏰ Напоминания:\n"
        "Нет запланированных напоминаний"
    )


def test_place_and_date_block_omitted_entirely_when_neither_is_known():
    """R7's "nothing invented" applies here too — a line for a fact nobody
    has stated is worse than no line at all."""
    result = render_status(
        place=None, event_date=None,
        participants_rendered="Пока никого не записал.",
        list_rendered="Список пока пуст.",
        reminders_rendered="Нет запланированных напоминаний",
    )

    assert "📍" not in result
    assert "📅" not in result
    assert result.startswith("Вот текущая информация по организации встречи:\n\n👥")


def test_only_the_known_half_of_place_and_date_is_shown():
    only_place = render_status(
        place="Tel Aviv", event_date=None,
        participants_rendered="x", list_rendered="y", reminders_rendered="z",
    )
    only_date = render_status(
        place=None, event_date="20/11",
        participants_rendered="x", list_rendered="y", reminders_rendered="z",
    )

    assert "📍 Место: Tel Aviv" in only_place and "📅" not in only_place
    assert "📅 Дата: 20/11" in only_date and "📍" not in only_date


def test_empty_reminders_renders_the_exact_requested_line():
    assert render_reminders([]) == "Нет запланированных напоминаний"


def test_a_reminder_is_rendered_with_its_message_and_time():
    result = render_reminders([
        {"message": "спросить Свету", "next_at": "2026-11-20T09:00:00",
         "repeats_every_minutes": None, "repeats_until": None, "target": "группа"},
    ])

    assert "спросить Свету" in result
    assert "2026-11-20T09:00:00" in result
    assert "группа" not in result  # the default target is not called out by name


def test_a_targeted_reminder_names_who_it_goes_to():
    result = render_reminders([
        {"message": "напомни Вите", "next_at": "2026-11-20T09:00:00",
         "repeats_every_minutes": None, "repeats_until": None, "target": "Витька"},
    ])

    assert "Витька" in result


def test_the_whole_report_survives_to_plain_text_unchanged():
    """Same guarantee every other rendered block in this project has: nothing
    in S1's converter touches these lines, so it can never silently reformat
    the report it is supposed to relay verbatim."""
    report = render_status(
        place="Tel Aviv, sea", event_date="20/11",
        participants_rendered="◻️ Витька\n✅ Андрюха",
        list_rendered="Ещё не разобрали:\n◻️ пиво",
        reminders_rendered="Нет запланированных напоминаний",
    )

    assert to_plain_text(report) == report
