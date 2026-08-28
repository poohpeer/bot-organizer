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


def as_amounts(entries) -> list[dict]:
    """A model-supplied list of {amount, unit} cleaned into storable pairs.

    Anything without a usable number is dropped rather than stored as a zero,
    and a unit named twice is collapsed onto its last value — "ящик и две
    бутылки, нет, три" is one restatement, not two contradictory ones.
    """
    out: list[dict] = []
    for entry in entries or []:
        if not isinstance(entry, dict):
            continue
        try:
            value = float(entry.get("amount"))
        except (TypeError, ValueError):
            continue
        unit = normalize_unit(entry.get("unit"))
        for existing in out:
            if existing["unit"] == unit:
                existing["amount"] = value
                break
        else:
            out.append({"amount": value, "unit": unit})
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
