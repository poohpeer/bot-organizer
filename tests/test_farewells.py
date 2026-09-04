"""The sign-off: one fixed line, then one of many."""

import random

from bot.farewells import CLOSING_LINE, FAREWELLS, closing_message


def test_every_message_opens_the_same_way():
    """The fixed half is the bot's signature — a session that ended reads as
    ended because of it, whichever farewell followed."""
    assert all(closing_message().startswith(CLOSING_LINE) for _ in range(50))


def test_the_farewell_actually_varies():
    """Thirty picnics should not close with the words of the first."""
    seen = {closing_message() for _ in range(200)}

    assert len(seen) > 1


def test_every_phrase_is_reachable():
    """A tuple long enough to hide a typo'd index — this fails if the choice
    is ever narrowed to a slice of the list."""
    pinned = {closing_message(rng=random.Random(seed)) for seed in range(2000)}

    assert len(pinned) == len(FAREWELLS)


def test_the_choice_can_be_pinned():
    """So a caller's test can assert an exact string without reaching into
    this module's globals."""
    once = closing_message(rng=random.Random(7))
    again = closing_message(rng=random.Random(7))

    assert once == again


def test_the_phrases_are_distinct_and_look_like_sentences():
    assert len(set(FAREWELLS)) == len(FAREWELLS)
    assert all(p == p.strip() and p[-1] in ".!" for p in FAREWELLS)


def test_the_two_halves_are_separated():
    """Run together they would read as one malformed sentence."""
    tail = closing_message(rng=random.Random(1))[len(CLOSING_LINE):]

    assert tail.startswith(" ")
    assert tail.strip() in FAREWELLS
