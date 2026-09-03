from bot.formatting import to_plain_text
from bot.list_render import LIST_CATEGORIES, render


def _item(name, category, claimed_by=None, quantity=None):
    return {"name": name, "category": category, "claimed_by": claimed_by, "quantity": quantity}


def test_all_three_sort_keys_exercised_at_once():
    """Dropping any one of the three keys changes this result:

    - claim status: "Утка" is claimed and must move to the second section even
      though its category (мясо) would otherwise sort it right next to the
      other мясо item;
    - category order: мясо (index 0) must come before бакалея (index 5) in the
      fixed vocabulary, the opposite of their alphabetical order;
    - name order: "антилопа" must come before "Ягнёнок" once case is folded —
      without str.lower(), Cyrillic uppercase codepoints sort *before* all
      lowercase ones, so a naive sort would put "Ягнёнок" first.
    """
    items = [
        _item("Ягнёнок", "мясо"),
        _item("антилопа", "мясо"),
        _item("мука", "бакалея"),
        _item("Утка", "мясо", claimed_by="Дима"),
    ]

    assert render(items) == (
        "Ещё не разобрали:\n"
        "◻️ антилопа\n"
        "◻️ Ягнёнок\n"
        "◻️ мука\n"
        "\n"
        "Уже взяли:\n"
        "✅ Утка — Дима"
    )


def test_empty_list_renders_a_readable_message_not_an_empty_string():
    result = render([])

    assert result != ""
    assert "пуст" in result.lower()


def test_missing_quantity_has_no_trailing_comma_and_never_says_none():
    result = render([_item("хлеб", "хлеб и выпечка")])

    assert result == "Ещё не разобрали:\n◻️ хлеб"
    assert "None" not in result
    assert "," not in result


def test_claimed_item_shows_the_claimants_name():
    result = render([_item("вода", "напитки", claimed_by="Alex", quantity="6 бутылок")])

    assert result == "Уже взяли:\n✅ вода, 6 бутылок — Alex"


def test_claimed_section_omitted_when_nothing_is_claimed():
    result = render([_item("хлеб", "хлеб и выпечка")])

    assert "Уже взяли" not in result


def test_unclaimed_section_omitted_when_everything_is_claimed():
    result = render([_item("вода", "напитки", claimed_by="Alex")])

    assert "Ещё не разобрали" not in result


def test_item_names_with_markup_characters_survive_verbatim():
    """There is no markup here to escape — mangling <, * or _ would be a bug,
    not a safety measure."""
    items = [
        _item("chips <spicy>", "бакалея"),
        _item("pa_sta", "бакалея"),
        _item("a*b", "бакалея"),
    ]

    result = render(items)

    assert "chips <spicy>" in result
    assert "pa_sta" in result
    assert "a*b" in result


def test_rendered_output_survives_to_plain_text_unchanged():
    """The guard that S1 and S3 cannot fight each other: the em dash this
    module writes is deliberately not "-", which to_plain_text would have
    rewritten (into... an em dash), corrupting nothing here but silently
    double-processing every line it touches."""
    items = [
        _item("Ягнёнок", "мясо"),
        _item("антилопа", "мясо"),
        _item("вода", "напитки", claimed_by="Alex", quantity="6 бутылок"),
        _item("chips <spicy>", "бакалея"),
        _item("pa_sta", "прочее"),
    ]

    rendered = render(items)

    assert to_plain_text(rendered) == rendered


def test_unknown_or_missing_category_sorts_as_prochee():
    """A NULL category (a row from before this column existed) or an
    unrecognized one must not crash the sort — it lands in прочее, at the end
    of the fixed vocabulary's order."""
    items = [
        _item("сюрприз", None),
        _item("гвозди", "хозтовары"),
        _item("мясо для шашлыка", "мясо"),
    ]

    result = render(items)

    assert result == (
        "Ещё не разобрали:\n"
        "◻️ мясо для шашлыка\n"
        "◻️ гвозди\n"
        "◻️ сюрприз"
    )


def test_no_category_names_appear_anywhere_in_the_output():
    """The category vocabulary still decides sort order — it is never printed
    as a heading. A reader who reintroduces the groupby-and-label step this
    module used to have would break this."""
    items = [
        _item("говядина", "мясо"),
        _item("молоко", "молочка"),
        _item("непонятно что", None),
    ]

    result = render(items)

    for category in LIST_CATEGORIES:
        assert category.capitalize() not in result


def test_whoever_took_an_item_is_named_by_handle():
    rendered = render(
        [{"name": "хлеб", "claimed_by": "Alex", "claimed_by_username": "poohpeer"}]
    )

    assert "@poohpeer" in rendered
    assert "Alex" not in rendered


def test_without_a_handle_the_claimer_keeps_their_name():
    rendered = render([{"name": "хлеб", "claimed_by": "Игорёк"}])

    assert "Игорёк" in rendered
