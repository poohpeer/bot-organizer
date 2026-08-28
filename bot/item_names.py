"""Turning what someone said into the name an item is stored under.

Three steps, in this order, all pure functions over strings:

    "2 пачки макарон" -> strip quantity -> "макарон" -> nominative -> "макароны"

Kept out of bot/tools/core.py because none of it touches the database, and
because the same functions have to run on both sides of every lookup: an item
stored under its normalized name is only findable if the query is normalized
the same way. That symmetry is the whole reason this is safe — the earlier
rule against rewriting item names existed because rewriting on write while
matching on the raw text loses the item.
"""

import functools
import re

# Quantity words the model routinely leaves glued to the item name. Only units
# and bare numerals — never a word that could be the item itself, which is why
# "бутылка" is here but "вода" is not: "бутылка воды" is water, while a lone
# "бутылка" the group actually wants is not something this list needs to
# distinguish. _strip never removes the last remaining word, so that case
# survives regardless.
_QUANTITY_WORDS = frozenset({
    "кг", "килограмм", "килограмма", "килограммов", "г", "гр", "грамм",
    "грамма", "граммов", "л", "литр", "литра", "литров", "мл",
    "шт", "штук", "штуки", "штука", "пачка", "пачки", "пачек",
    "бутылка", "бутылки", "бутылок", "банка", "банки", "банок",
    "упаковка", "упаковки", "пара", "пары", "пару", "коробка", "коробки",
    "ящик", "ящика", "ящиков", "мешок", "мешка", "мешков",
    "булка", "булки", "булок", "буханка", "буханки", "буханок",
    "кусок", "куска", "кусков", "рулон", "рулона", "рулонов",
})

_NUMERAL_WORDS = frozenset({
    "один", "одна", "одно", "два", "две", "три", "четыре", "пять", "шесть",
    "семь", "восемь", "девять", "десять", "несколько", "немного", "пара",
    "чуть", "побольше", "поменьше", "пол", "полкило",
    # Hyphenated half-measures the analyzer does not reduce to anything in
    # the list above.
    "пол-литра", "поллитра", "пол-кило", "полкило", "полтора", "полторы",
})

_CYRILLIC = re.compile(r"[а-яё]", re.IGNORECASE)

# "1кг", "500г", "2л" — a number written against its unit with no space.
# Whitespace tokenising keeps that as one token, which is neither a digit nor
# a listed word, so it survived into the name: one live list showed
# "1кг апельсины".
_GLUED_AMOUNT = re.compile(r"^\d+[.,]?\d*(?=[а-яё])", re.IGNORECASE)


@functools.lru_cache(maxsize=1)
def _analyzer():
    """Loaded once, on first use.

    Building a MorphAnalyzer reads the whole dictionary and takes about a
    second, which is fine once and not fine per list_add. Lazy rather than at
    import so nothing that never touches item names pays for it.
    """
    import pymorphy3

    return pymorphy3.MorphAnalyzer()


def _bare(token: str) -> str:
    return token.strip(".,;:!?()\"'")


def _tokens(name: str) -> list[str]:
    """Split on whitespace, and additionally where a number runs straight
    into a word: "1кг" becomes "1", "кг"."""
    out = []
    for token in name.split():
        match = _GLUED_AMOUNT.match(token)
        if match:
            out.append(match.group())
            out.append(token[match.end():])
        else:
            out.append(token)
    return out


def _is_amount(word: str) -> bool:
    """Whether a word is about how much, rather than about what.

    Checks the lemma as well as the word itself. canonical() normalises
    before stripping, which turns "бутылку" into "бутылка" and catches it —
    but it also turns "килограмм" into the plural "килограммы", which is a
    form nobody thought to list. The lemma of both is "килограмм", so asking
    for that instead of enumerating every number and case is what makes the
    list finite.
    """
    if not word:
        return False
    lowered = word.lower()
    if lowered in _QUANTITY_WORDS or lowered in _NUMERAL_WORDS:
        return True
    if lowered.replace(",", ".").replace(".", "").isdigit():
        return True
    if not _CYRILLIC.search(lowered):
        return False
    parsed = _analyzer().parse(lowered)
    if not parsed:
        return False
    lemma = parsed[0].normal_form
    return lemma in _QUANTITY_WORDS or lemma in _NUMERAL_WORDS


def strip_quantity(name: str) -> str:
    """Drop quantity words from an item name.

    The model puts the amount in both places: list_add(name="2 кг мяса",
    quantity="2 кг"). Left alone that is a different string from the same item
    added as "мяса", so the two sit in the list as separate rows — exactly what
    happened live when one message reached both the silent-capture and the
    addressed path.
    """
    if not isinstance(name, str):
        return name
    kept = [token for token in _tokens(name) if not _is_amount(_bare(token))]
    # Never leave nothing behind: a group asking for "бутылка" gets a bottle,
    # not a row called "".
    return " ".join(kept).strip() if kept else name.strip()


def to_nominative(name: str) -> str:
    """Put a Russian item name into the nominative case.

    "мяса" -> "мясо", "2 пачки макарон" -> "макароны", "свежих помидоров" ->
    "свежие помидоры"; adjectives agree because each word is inflected on its
    own and they were already in agreement.

    Inflected to nominative rather than lemmatised. The lemma of "огурцы" is
    "огурец", so lemmatising would quietly turn a group's plural into a
    singular they never wrote; inflecting keeps number, so "огурцы" stays
    "огурцы" and "помидоров" becomes "помидоры".

    A capitalised word is left exactly as written. In practice those are
    proper nouns and brands — "Ben Shemen", "Кока-кола", "поляна Ханания" —
    and the analyzer is happy to mangle them ("Ханания" -> "хананий", scored
    0.25). Items are written in lower case, so the rule costs nothing and
    removes the one class of damage worth fearing here.
    """
    if not isinstance(name, str) or not name.strip():
        return name

    analyzer = _analyzer()
    out = []
    for token in name.split():
        bare = _bare(token)
        if not bare or bare[:1].isupper() or not _CYRILLIC.search(bare):
            out.append(token)
            continue
        parsed = analyzer.parse(bare)
        if not parsed:
            out.append(token)
            continue
        inflected = parsed[0].inflect({"nomn"})
        # inflect() returns None for anything it cannot decline — foreign
        # words, interjections, forms it does not know. Leaving the token as
        # written is always safe; guessing is not.
        out.append(token.replace(bare, inflected.word) if inflected else token)
    return " ".join(out)


def canonical(name: str) -> str:
    """The name an item is stored under: nominative case, no quantity.

    Live, "одну бутылку чая" kept every word and the list gained an item
    called "одна бутылка чай": the word lists are written in the nominative,
    so "одну" and "бутылку" walked straight past them. What fixes that is
    _is_amount checking each word's lemma, not the order of the two steps —
    verified by running both orders over every case in tests/test_item_names.py
    and getting identical output. Normalising first is kept because it leaves
    _is_amount on its fast path for most words.
    """
    return strip_quantity(to_nominative(name))


def _lemma(word: str) -> str:
    if not _CYRILLIC.search(word):
        return word
    parsed = _analyzer().parse(word)
    return parsed[0].normal_form if parsed else word


def match_key(name: str) -> str:
    """What makes two item names the same item.

    Compared by lemma, so number stops mattering: someone asking for "банан"
    when the list already says "бананы" is asking about the same fruit, and
    the two used to sit as separate rows. Order- and case-insensitive too, so
    "2 кг мяса", "мяса, 2 кг" and "мясо" all collide.

    The lemma belongs here and nowhere else. Using it for the *stored* name
    would rewrite a group's "огурцы" into "огурец" — a plural they never
    wrote — which is why canonical() inflects to the nominative and keeps
    number instead. Matching is machinery nobody reads; the name is not.
    """
    return " ".join(sorted(
        _lemma(_bare(word).lower())
        for word in strip_quantity(name or "").split() if _bare(word)
    ))
