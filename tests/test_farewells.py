"""The sign-off: one line, drawn from one list."""

import random

from bot.farewells import FAREWELLS, closing_message

MOOR = "Мавр сделал своё дело, мавр может уходить."


def test_the_message_is_one_phrase_and_nothing_else():
    """Not a fixed line plus a rotating tail. Said before every other
    farewell, the Moor stopped being a sign-off and became a preamble."""
    assert all(closing_message() in FAREWELLS for _ in range(100))


def test_the_moor_is_a_member_not_a_prefix():
    assert MOOR in FAREWELLS
    assert not all(closing_message().startswith(MOOR) for _ in range(50))


def test_the_moor_comes_up_about_as_often_as_the_rest():
    """It leads the list because people know it, but it is an ordinary member
    — roughly one closing in thirty-one, not every one."""
    draws = [closing_message(rng=random.Random(seed)) for seed in range(3100)]
    moors = draws.count(MOOR)

    assert 40 < moors < 160, f"the Moor came up {moors} times in 3100"


def test_every_phrase_is_reachable():
    """A list long enough to hide a typo'd index — this fails if the choice
    is ever narrowed to a slice of it."""
    pinned = {closing_message(rng=random.Random(seed)) for seed in range(2000)}

    assert len(pinned) == len(FAREWELLS)


def test_the_choice_actually_varies():
    """Thirty-one picnics should not close with the words of the first."""
    assert len({closing_message() for _ in range(200)}) > 1


def test_the_choice_can_be_pinned():
    """So a caller's test can assert an exact string without reaching into
    this module's globals."""
    assert closing_message(rng=random.Random(7)) == closing_message(rng=random.Random(7))


def test_the_phrases_are_distinct_and_look_like_sentences():
    assert len(set(FAREWELLS)) == len(FAREWELLS)
    assert all(p == p.strip() and p[-1] in ".!" for p in FAREWELLS)
