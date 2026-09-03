"""Renders the shared list as grouped plain text (R4).

No table, and no `parse_mode`: an aligned table needs a monospace block,
which needs HTML, which means every `<` in an item name has to be escaped or
Telegram drops the whole message — a worse failure than ragged columns, since
the group would see nothing at all. Grouped plain text reads correctly in any
font and cannot fail to send, and keeps the bot to the one outgoing format S1
already guarantees.

Each item's leading marker is a pure function of its current claimed state —
there is no cached rendering anywhere, so an icon is always current as of the
call that produced it, and "update the icons when a status changes" is not a
separate mechanism to get right, just a consequence of calling render() again.
"""

from bot import people


# Fixed and closed: sorting has to be stable across calls, and a model asked
# to invent categories will say "молочка" once and "молочные продукты" the
# next time, which sorts differently and looks broken. Order matters and is
# not alphabetical — this is the order people actually shop in.
LIST_CATEGORIES = (
    "мясо", "молочка", "овощи и фрукты", "напитки", "хлеб и выпечка",
    "бакалея", "посуда", "прочее",
)
DEFAULT_CATEGORY = "прочее"

_CATEGORY_RANK = {name: i for i, name in enumerate(LIST_CATEGORIES)}
_DEFAULT_RANK = _CATEGORY_RANK[DEFAULT_CATEGORY]

_UNCLAIMED_HEADING = "Ещё не разобрали:"
_CLAIMED_HEADING = "Уже взяли:"

# Nobody has added anything yet — an empty string would look like a bug to a
# human reading the chat, and the silence guard in router.py only swallows a
# reply that is *entirely* invisible, so "" would still get posted as nothing.
_EMPTY_LIST = "Список пока пуст."


def _category_rank(item: dict) -> int:
    # Anything not in the fixed vocabulary — an invented category, or a NULL
    # from a row added before this column existed — sorts as прочее.
    return _CATEGORY_RANK.get(item.get("category"), _DEFAULT_RANK)


def _sort_key(item: dict):
    return (_category_rank(item), item["name"].lower())


_TAKEN = "✅"      # someone has claimed it
_NOT_TAKEN = "◻️"  # nobody has claimed it yet

# Neither is *, - or + at a line start, so bot.formatting.to_plain_text's
# bullet rewrite never touches these lines — same reasoning that picked the em
# dash before, still holds for an emoji marker.


def _item_line(item: dict) -> str:
    marker = _TAKEN if item.get("claimed_by") else _NOT_TAKEN
    line = f"{marker} {item['name']}"
    if item.get("quantity"):
        line += f", {item['quantity']}"
    if item.get("claimed_by"):
        # claimed_by_username is not a column — it is filled in by the caller
        # from the session roster (see bot/tools/core.py list_show), because
        # the handle belongs to the person, not to the item, and duplicating
        # it here would leave a stale copy behind the day someone renames.
        line += f" — {people.mention(item['claimed_by'], item.get('claimed_by_username'))}"
    return line


def _section(heading: str, items: list[dict]) -> str:
    # Sorted by category rank, but the category name itself is never printed
    # — the fixed vocabulary decides the *order* items appear in, not a label
    # on the page. groupby is gone with it: there is nothing left to group by
    # for, just one line per item in sort order.
    lines = [heading]
    lines.extend(_item_line(item) for item in sorted(items, key=_sort_key))
    return "\n".join(lines)


def render(items: list[dict]) -> str:
    if not items:
        return _EMPTY_LIST

    unclaimed = [i for i in items if not i.get("claimed_by")]
    claimed = [i for i in items if i.get("claimed_by")]

    # A section that would be empty is omitted entirely, heading and all — a
    # list where nobody has taken anything must not end with a bare
    # "Уже взяли:".
    sections = []
    if unclaimed:
        sections.append(_section(_UNCLAIMED_HEADING, unclaimed))
    if claimed:
        sections.append(_section(_CLAIMED_HEADING, claimed))
    return "\n\n".join(sections)
