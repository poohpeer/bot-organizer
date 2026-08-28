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
    "ящик": "ящ.",
    "мешок": "меш.",
    "булка": "бул.",
    "кусок": "кус.",
    "рулон": "рул.",
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


# How several amounts of one item are joined. Asked for a bottle of wine when
# a crate is listed, the list says "1 ящ. + 1 бут." — keeping both rather than
# picking one, because neither is wrong and dropping either loses something
# somebody said.
_JOIN = " + "


def combine(amounts) -> str | None:
    """Several (amount, unit) pairs as one string, in the order they were
    added. Same-unit amounts are summed before they get here, so a unit never
    appears twice."""
    if not amounts:
        return None
    parts = [
        rendered for rendered in
        (render(entry.get("amount"), entry.get("unit")) for entry in amounts)
        if rendered
    ]
    return _JOIN.join(parts) if parts else None


def add_to(amounts, amount, unit) -> list[dict]:
    """Fold a new amount into what an item already holds.

    A different unit is kept alongside: a crate and a bottle are both real,
    and converting between them would be inventing a rate nobody gave.

    The same unit replaces rather than sums, which is the one part of this
    nobody has specified. Summing reads better for "добавь ещё два литра",
    but one message has already been seen reaching both the silent-capture
    and the addressed path, and under summing that duplicate delivery would
    quietly double the amount — a wrong number nobody can see is wrong.
    Replacing is the recoverable failure: say it again and it is right.
    """
    if amount is None:
        return list(amounts or [])
    unit = normalize_unit(unit)
    out = [dict(entry) for entry in (amounts or [])]
    for entry in out:
        if normalize_unit(entry.get("unit")) == unit:
            entry["amount"] = float(amount)
            return out
    out.append({"amount": float(amount), "unit": unit})
    return out


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
