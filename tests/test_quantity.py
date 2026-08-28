"""Amount rendering: a numeral and the unit's abbreviation."""

import bot.quantity as quantity


def test_the_shapes_that_were_asked_for():
    assert quantity.render(2, "килограмм") == "2 кг"
    assert quantity.render(0.5, "килограмм") == "0.5 кг"
    assert quantity.render(1, "бутылка") == "1 бут."
    assert quantity.render(1, "штука") == "1 шт."


def test_an_abbreviation_reads_the_same_after_any_number():
    """Why this file has no agreement table. The spelled-out version needed
    three forms per unit — килограмм / килограмма / килограммов — plus a
    special case for the teens, where "11 килограмма" is the mistake waiting
    to happen."""
    for n in (1, 2, 5, 11, 21, 0.5):
        assert quantity.render(n, "килограмм").endswith(" кг")
        assert quantity.render(n, "штука").endswith(" шт.")


def test_the_number_stays_a_numeral():
    """"0.5 кг" beats "ноль целых пять десятых килограмма" at the one job a
    shopping list has."""
    assert quantity.render(0.5, "килограмм") == "0.5 кг"
    assert quantity.render(1.5, "литр") == "1.5 л"


def test_a_whole_number_loses_its_decimal_point():
    """The column is NUMERIC, so 2 arrives as 2.0."""
    assert quantity.render(2.0, "литр") == "2 л"
    assert quantity.render(1.0, "бутылка") == "1 бут."


def test_every_unit_in_the_vocabulary_has_an_abbreviation():
    """UNITS is what the model is told to choose from, so a name that reaches
    render() without a form here would be a KeyError in production."""
    for unit in quantity.UNITS:
        assert quantity.render(1, unit) is not None


def test_an_unknown_unit_becomes_a_plain_count():
    assert quantity.render(3, "вёдер") == "3 шт."
    assert quantity.render(3, None) == "3 шт."


def test_no_amount_renders_as_nothing():
    """So an item nobody gave an amount for stays bare, rather than gaining a
    "1 шт." nobody said."""
    assert quantity.render(None, "килограмм") is None
    assert quantity.render("не число", "килограмм") is None


def test_a_crate_has_an_abbreviation():
    """Live: "ящик вина" had nowhere to put the crate, so it stayed in the
    name and the list showed "ящик вино"."""
    assert quantity.render(1, "ящик") == "1 ящ."
    assert quantity.render(2, "мешок") == "2 меш."


def test_a_different_unit_is_kept_alongside():
    """Asked for a bottle of wine with a crate already listed, the answer is
    both — converting between them would invent a rate nobody gave."""
    amounts = quantity.add_to([], 1, "ящик")
    amounts = quantity.add_to(amounts, 1, "бутылка")

    assert quantity.combine(amounts) == "1 ящ. + 1 бут."


def test_the_same_unit_replaces_rather_than_sums():
    """The one part nobody specified. Summing reads better for "добавь ещё
    два литра", but one message has been seen reaching both the
    silent-capture and the addressed path, and summing would quietly double
    the amount — a wrong number nobody can see is wrong. Replacing is the
    recoverable failure: say it again and it is right."""
    amounts = quantity.add_to([], 2, "литр")
    amounts = quantity.add_to(amounts, 3, "литр")

    assert quantity.combine(amounts) == "3 л"
    assert len(amounts) == 1


def test_units_keep_the_order_they_were_added_in():
    amounts = quantity.add_to([], 1, "ящик")
    amounts = quantity.add_to(amounts, 2, "бутылка")
    amounts = quantity.add_to(amounts, 0.5, "килограмм")

    assert quantity.combine(amounts) == "1 ящ. + 2 бут. + 0.5 кг"


def test_nothing_to_combine_renders_as_nothing():
    assert quantity.combine([]) is None
    assert quantity.combine(None) is None


def test_an_amount_of_none_is_ignored_rather_than_stored():
    """The model is asked for a number and does not always send one."""
    assert quantity.add_to([{"amount": 1, "unit": "ящик"}], None, "бутылка") == [
        {"amount": 1, "unit": "ящик"}
    ]
