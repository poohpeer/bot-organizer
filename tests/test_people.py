"""How a person is named in anything the group reads."""

from bot import people


def test_a_handle_wins_over_the_profile_name():
    """The '@' is not decoration: Telegram turns it into a live mention, so
    the person actually gets notified. A display name notifies nobody."""
    assert people.mention("Alex", "poohpeer") == "@poohpeer"


def test_an_at_already_there_is_not_doubled():
    assert people.mention("Alex", "@poohpeer") == "@poohpeer"


def test_without_a_handle_the_name_is_used_as_written():
    assert people.mention("Игорёк", None) == "Игорёк"


def test_a_name_is_never_turned_into_a_handle():
    """Guessing one would mention whoever really owns that handle."""
    assert not people.mention("poohpeer", None).startswith("@")


def test_neither_known_still_renders_something():
    """A renderer must not produce a bare gap in the middle of a list."""
    assert people.mention(None, None) == "?"


def test_normalise_strips_the_at_so_lookups_match():
    """Usernames arrive both ways — from from_user.username without the '@',
    and from a model copying "@poohpeer" out of a message. Stored both ways,
    the same handle would occupy two rows."""
    assert people.normalise_username("@poohpeer") == "poohpeer"
    assert people.normalise_username("poohpeer") == "poohpeer"


def test_normalise_treats_nothing_as_nothing():
    assert people.normalise_username(None) is None
    assert people.normalise_username("   ") is None
    assert people.normalise_username("@") is None


# --- counting people ------------------------------------------------------

def test_the_bot_is_not_one_of_the_people_in_the_chat():
    """getChatMemberCount counts the bot as a member, so every sentence built
    from it would be one person too many."""
    assert people.humans(9) == 8
    assert people.humans(1) == 0


def test_an_unfetched_count_stays_unknown():
    """None is "never fetched", not "nobody there" — the caller has to be
    able to say nothing rather than claim an empty group."""
    assert people.humans(None) is None


def test_a_count_never_goes_negative():
    assert people.humans(0) == 0


def test_the_noun_agrees_with_the_number():
    assert people.how_many(1) == "1 человек"
    assert people.how_many(2) == "2 человека"
    assert people.how_many(4) == "4 человека"
    assert people.how_many(5) == "5 человек"
    assert people.how_many(21) == "21 человек"
    assert people.how_many(22) == "22 человека"


def test_the_teens_do_not_follow_their_last_digit():
    """11-14 take the same form as 5+ despite ending in 1-4 — the case a
    naive `count == 1` check gets wrong."""
    assert people.how_many(11) == "11 человек"
    assert people.how_many(12) == "12 человек"
    assert people.how_many(14) == "14 человек"
    assert people.how_many(112) == "112 человек"
