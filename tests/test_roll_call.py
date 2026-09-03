"""The roll call's wording and its schedule."""

import bot.roll_call as roll_call


def _p(name, status="unknown", username=None):
    return {"display_name": name, "status": status, "username": username}


# --- the schedule ---------------------------------------------------------

def test_the_gap_doubles_each_round():
    assert roll_call.next_interval_minutes(0) == 60
    assert roll_call.next_interval_minutes(1) == 120
    assert roll_call.next_interval_minutes(2) == 240


def test_three_rounds_and_no_more():
    assert roll_call.MAX_ROUNDS == 3


def test_a_round_past_the_last_still_gets_a_sane_gap():
    """The run ends by rounds_done, not by the clock, so an out-of-range
    index must clamp rather than raise and strand the row."""
    assert roll_call.next_interval_minutes(99) == roll_call.ROUND_INTERVALS_MINUTES[-1]


# --- who is left to ask ---------------------------------------------------

def test_a_hedged_reply_counts_as_answered():
    """Someone who said "может быть" has replied, just non-committally.
    Asking them again chases a response they already gave."""
    answered, unanswered = roll_call.split_by_answer(
        [_p("A", "maybe"), _p("B", "unknown")]
    )

    assert [p["display_name"] for p in answered] == ["A"]
    assert [p["display_name"] for p in unanswered] == ["B"]


def test_declining_counts_as_answered_too():
    answered, _ = roll_call.split_by_answer([_p("A", "declined")])

    assert [p["display_name"] for p in answered] == ["A"]


# --- how many people the bot has never heard of ---------------------------

def test_the_bot_does_not_count_itself_as_a_stranger():
    """getChatMemberCount counts the bot. Without the -1 it would report one
    unknown person in a chat where it knows everybody."""
    assert roll_call.unknown_others(chat_member_count=4, recorded_count=3) == 0


def test_extra_members_are_counted():
    assert roll_call.unknown_others(chat_member_count=10, recorded_count=3) == 6


def test_an_unfetched_member_count_claims_nothing():
    """None means never fetched. Reporting 0 there would state as fact that
    the bot knows everyone, which is the one thing it can never know."""
    assert roll_call.unknown_others(chat_member_count=None, recorded_count=3) == 0


def test_a_roster_larger_than_the_chat_does_not_go_negative():
    assert roll_call.unknown_others(chat_member_count=2, recorded_count=9) == 0


# --- the question ---------------------------------------------------------

def test_the_question_names_everyone_still_silent():
    text = roll_call.render_question([_p("Игорёк"), _p("Бердыев")])

    assert "Игорёк" in text
    assert "Бердыев" in text


def test_the_question_uses_the_handle_so_telegram_notifies():
    text = roll_call.render_question([_p("Alex", username="poohpeer")])

    assert "@poohpeer" in text
    assert "Alex" not in text


def test_the_blind_ask_appears_only_when_someone_is_unaccounted_for():
    """There is no way to enumerate a group's members, so asking the room at
    large is the only way to reach the people the bot cannot name."""
    with_others = roll_call.render_question([_p("A")], others=3)
    without = roll_call.render_question([_p("A")], others=0)

    assert "остальные" in with_others
    assert "остальные" not in without


# --- the summary ----------------------------------------------------------

def test_the_summary_gives_both_lists_and_the_handover():
    text = roll_call.render_summary(
        [_p("Игорёк", "confirmed")], [_p("Бердыев")]
    )

    assert "Ответили:" in text
    assert "Игорёк — иду" in text
    assert "Не ответили:" in text
    assert "Бердыев" in text
    assert "Позаботьтесь, чтобы все ответили." in text


def test_each_answer_is_spelled_out_not_left_as_an_icon():
    text = roll_call.render_summary(
        [_p("A", "confirmed"), _p("B", "maybe"), _p("C", "declined")], []
    )

    assert "A — иду" in text
    assert "B — может быть" in text
    assert "C — не иду" in text


def test_the_strangers_line_appears_when_the_chat_is_bigger():
    text = roll_call.render_summary([_p("A", "confirmed")], [], others=4)

    assert "ещё 4 человек" in text


def test_the_strangers_line_is_absent_when_nobody_is_unaccounted_for():
    text = roll_call.render_summary([_p("A", "confirmed")], [], others=0)

    assert "не знаю" not in text


def test_an_empty_side_says_so_rather_than_vanishing():
    """A missing heading reads as the bot having forgotten to check, which is
    exactly the doubt the summary exists to remove."""
    nobody_answered = roll_call.render_summary([], [_p("A")])
    everyone_answered = roll_call.render_summary([_p("A", "confirmed")], [])

    assert "Ответили:" in nobody_answered and "Никто не ответил." in nobody_answered
    assert "Не ответили:" in everyone_answered and "Ответили все." in everyone_answered
