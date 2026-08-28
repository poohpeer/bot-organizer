"""How much of something, written the same way however it was said.

The model supplies a number and a unit; this pairs the number with the unit's
abbreviation — "2 кг", "0.5 кг", "1 шт.", "1 бут.". Asked for "2 кг мяса",
"одну бутылку чая" and "полкило помидоров", the free-text quantity field had
stored exactly those three shapes and the list showed all three.

Abbreviations rather than full words, which is what makes this file short:
"кг" and "шт." read the same after any number, so there is no agreement to
get right. The spelled-out version needed a three-form table per unit —
килограмм / килограмма / килограммов — plus a special case for the teens,
and pymorphy3 could not be trusted to generate it (asked to agree
"килограмм" with 1 it answers "килограмма", and it parses "банка" as a
masculine "банк", yielding "банков").

The number stays a numeral for the same reason: "0.5 кг" beats "ноль целых
пять десятых килограмма" at the one job a shopping list has.
"""

# The vocabulary the model chooses from, and how each one is shown.
_UNITS = {
    "штука": "шт.",
    "килограмм": "кг",
    "грамм": "г",
    "литр": "л",
    "миллилитр": "мл",
    "бутылка": "бут.",
    "пачка": "пач.",
    "банка": "бан.",
    "упаковка": "упак.",
    "коробка": "кор.",
}

UNITS = tuple(_UNITS)
# A count of things with no natural unit is a count of штуки. Also where an
# unrecognised unit lands, rather than being rejected: refusing would cost a
# whole turn to fix a cosmetic detail.
DEFAULT_UNIT = "штука"


def normalize_unit(unit) -> str:
    if isinstance(unit, str) and unit.strip().lower() in _UNITS:
        return unit.strip().lower()
    return DEFAULT_UNIT


def _number(amount: float) -> str:
    """2, not 2.0; 0.5, not 0.50."""
    if amount == int(amount):
        return str(int(amount))
    return f"{amount:g}"


def render(amount, unit) -> str | None:
    """"2 кг", "0.5 кг", "1 шт.", "1 бут.".

    None when there is no amount to show, so a caller can leave the item bare
    rather than printing a made-up "1 шт." nobody asked for.
    """
    if amount is None:
        return None
    try:
        value = float(amount)
    except (TypeError, ValueError):
        return None
    return f"{_number(value)} {_UNITS[normalize_unit(unit)]}"
