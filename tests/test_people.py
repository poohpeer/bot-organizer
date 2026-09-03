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
