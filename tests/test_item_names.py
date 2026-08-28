"""Item-name normalisation: quantity out, nominative in."""

import bot.item_names as item_names


def test_the_amount_leaves_the_name():
    assert item_names.canonical("2 кг мяса") == "мясо"
    assert item_names.canonical("килограмм помидоров") == "помидоры"
    assert item_names.canonical("2 пачки макарон") == "макароны"


def test_the_case_someone_spoke_in_is_not_part_of_the_name():
    """"Добавь мяса" and "мясо есть?" are about the same thing."""
    assert item_names.canonical("мяса") == "мясо"
    assert item_names.canonical("сыра") == "сыр"
    assert item_names.canonical("хлеба") == "хлеб"


def test_a_plural_the_group_wrote_stays_plural():
    """Inflected to nominative, not lemmatised. The lemma of "огурцы" is
    "огурец", so lemmatising would quietly turn a group's plural into a
    singular they never wrote."""
    assert item_names.canonical("огурцы") == "огурцы"
    assert item_names.canonical("помидоров") == "помидоры"
    assert item_names.canonical("яиц") == "яйца"


def test_adjectives_agree_with_the_noun():
    assert item_names.canonical("красного вина") == "красное вино"
    assert item_names.canonical("свежих помидоров") == "свежие помидоры"
    assert item_names.canonical("минеральной воды") == "минеральная вода"


def test_capitalised_words_are_left_exactly_as_written():
    """Proper nouns and brands. The analyzer is happy to mangle them —
    "Ханания" parses to "хананий" at 0.25 confidence — and items are written
    in lower case, so the rule costs nothing."""
    assert item_names.canonical("поляна Ханания") == "поляна Ханания"
    assert item_names.canonical("Кока-кола") == "Кока-кола"
    assert item_names.canonical("Тель-Авив") == "Тель-Авив"


def test_non_russian_names_are_untouched():
    assert item_names.canonical("сыр Ben Shemen") == "сыр Ben Shemen"
    assert item_names.canonical("Coca-Cola") == "Coca-Cola"


def test_stripping_never_empties_a_name():
    """A group asking for "бутылка" gets a bottle, not a row called ""."""
    assert item_names.canonical("бутылка") == "бутылка"
    assert item_names.canonical("пара") == "пара"


def test_the_same_item_said_three_ways_shares_one_key():
    """The live duplicate: one message reached both the silent-capture and the
    addressed path, and each extracted the amount differently."""
    keys = {
        item_names.match_key("2 кг мяса"),
        item_names.match_key("мяса, 2 кг"),
        item_names.match_key("мясо"),
    }
    assert len(keys) == 1


def test_word_order_does_not_change_the_key():
    assert item_names.match_key("красное вино") == item_names.match_key("вино красное")


def test_different_items_keep_different_keys():
    """The comparison has to stay narrow enough to tell things apart."""
    assert item_names.match_key("мясо") != item_names.match_key("молоко")
    assert item_names.match_key("красное вино") != item_names.match_key("белое вино")


def test_a_name_that_is_not_a_string_is_returned_unchanged():
    assert item_names.canonical(None) is None
    assert item_names.canonical("") == ""


def test_an_amount_spoken_in_any_case_still_leaves_the_name():
    """Live: "одну бутылку чая" kept every word and the list gained an item
    called "одна бутылка чай". The word lists are written in the nominative,
    so an amount spoken in the accusative slipped straight past them —
    canonical() normalises before stripping for exactly this reason."""
    assert item_names.canonical("одну бутылку чая") == "чай"
    assert item_names.canonical("две пачки чипсов") == "чипсы"
    assert item_names.canonical("пару бутылок вина") == "вино"
    assert item_names.canonical("пиццу") == "пицца"


def test_an_amount_in_the_plural_still_leaves_the_name():
    """Normalising first turns "килограмм" into the plural "килограммы",
    a form nobody thought to list. The lemma of both is "килограмм", which is
    why membership is tested against that."""
    assert item_names.canonical("килограмм помидоров") == "помидоры"
    assert item_names.canonical("пол-литра молока") == "молоко"
    assert item_names.canonical("полтора кг мяса") == "мясо"


def test_the_two_steps_commute():
    """The lemma check, not the ordering, is what makes case forms work. Worth
    pinning: a future reader who swaps these to save an analyzer call should
    find out here that it is safe, rather than guessing from a comment."""
    cases = [
        "одну бутылку чая", "2 кг мяса", "килограмм помидоров",
        "2 пачки макарон", "пару бутылок вина", "пол-литра молока",
        "бутылка", "огурцы", "красного вина",
    ]
    for name in cases:
        assert (item_names.strip_quantity(item_names.to_nominative(name))
                == item_names.to_nominative(item_names.strip_quantity(name))), name


def test_singular_and_plural_are_the_same_item():
    """Live: the model sent name="банан" while the list already said
    "бананы", and the two sat as separate rows. Someone asking for a banana
    when bananas are listed is asking about the same fruit."""
    assert item_names.match_key("банан") == item_names.match_key("бананы")
    assert item_names.match_key("огурец") == item_names.match_key("огурцы")
    assert item_names.match_key("помидоры") == item_names.match_key("2 кг помидоров")


def test_the_lemma_is_used_for_matching_only():
    """The lemma of "огурцы" is "огурец". Storing that would rewrite a
    group's plural into a singular they never wrote — which is why the name
    is inflected to the nominative and keeps number, while only the invisible
    match key is lemmatised."""
    assert item_names.canonical("огурцы") == "огурцы"
    assert item_names.canonical("бананы") == "бананы"
    assert item_names.match_key("огурцы") == item_names.match_key("огурец")


def test_different_things_still_do_not_collide():
    """Lemmatising loses information, so the comparison has to stay narrow
    enough to tell an item from a container."""
    assert item_names.match_key("банан") != item_names.match_key("банка")
    assert item_names.match_key("мясо") != item_names.match_key("молоко")
    assert item_names.match_key("красное вино") != item_names.match_key("белое вино")


def test_a_crate_is_an_amount_not_a_name():
    """Live: "ящик вина" reached the list as an item called "ящик вино"."""
    assert item_names.canonical("ящик вина") == "вино"
    assert item_names.canonical("мешок картошки") == "картошка"
