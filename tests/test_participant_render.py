from bot.formatting import to_plain_text
from bot.participant_render import render


def _p(display_name, status):
    return {"display_name": display_name, "status": status}


def test_every_status_gets_its_own_icon():
    result = render([
        _p("Андрюха", "confirmed"),
        _p("Витька", "unknown"),
        _p("Света", "maybe"),
        _p("Alex", "declined"),
    ])

    assert result == "✅ Андрюха\n◻️ Витька\n❓ Света\n❌ Alex"


def test_empty_roster_renders_a_readable_message_not_an_empty_string():
    result = render([])

    assert result != ""
    assert result != "\n"


def test_an_unrecognised_status_falls_back_to_the_no_response_icon_rather_than_raising():
    """A status this module doesn't know about — a future addition, or a row
    written before some status existed — must still render as something
    rather than crash the whole roster over one row."""
    result = render([_p("Кто-то", "some_future_status")])

    assert result == "◻️ Кто-то"


def test_rendered_roster_survives_to_plain_text_unchanged():
    """Same guarantee S1 gives the shopping list: nothing in bot.formatting
    touches these lines, so the two modules can never start fighting over
    the same message."""
    rendered = render([_p("Андрюха", "confirmed"), _p("Витька", "maybe")])

    assert to_plain_text(rendered) == rendered


def test_icons_are_a_pure_function_of_current_status():
    """"Update the icons when a status changes" is not a separate feature —
    render() has no memory of what it returned last time, so calling it again
    after a status change is the entire mechanism."""
    before = render([_p("Витька", "unknown")])
    after = render([_p("Витька", "confirmed")])

    assert "◻️ Витька" in before
    assert "✅ Витька" in after
    assert before != after
