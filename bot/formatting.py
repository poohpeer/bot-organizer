"""Converts Markdown the model writes into plain text for Telegram.

`parse_mode` is never set in `bot/` (see router.py), so every message goes out
as plain text and Telegram renders the model's Markdown literally. Rendering
it properly was rejected: Telegram drops the whole message on unbalanced
entities, and item names come from users, so one stray `*` in a list item
would lose the reply entirely. Converting to plain text first sidesteps that.
"""

import re

# List markers only count at line start, followed by a space — this is what
# tells "* хлеб" (a bullet) apart from "*Куда:*" (emphasis with no space
# after the opening marker). Indentation is captured and kept as-is.
_LIST_MARKER_RE = re.compile(r"^(\s*)[*\-+] (.*)$")

_HEADING_RE = re.compile(r"^#{1,6}\s+(.*)$")

# Emphasis markers are only removed in pairs on the same line, and only when
# three things hold at once:
#
#   * no whitespace touches the inside of either marker — a real emphasis run
#     never has a space right after the opening "*";
#   * the opening marker is not preceded by a word character;
#   * the closing marker is not followed by one.
#
# The last two are what keep markers that live *inside* a word from pairing up.
# Without them the converter eats things the bot writes constantly:
# "list_check_off" became "listcheckoff", "my_file_name.txt" became
# "myfilename.txt", a URL like ".../a_b_c" became ".../abc", and "5*4 и 3*2"
# became "54 и 32". CommonMark refuses intra-word "_" emphasis for the same
# reason; this applies the rule to "*" as well, since a chat bot writes far
# more identifiers and arithmetic than it writes emphasis.
#
# An unpaired marker ("звёздочка*") matches nothing here and survives — the R3
# case, and the reason this isn't `text.replace("*", "")`.
_OPEN = r"(?<![^\W_]|[*_])"
_CLOSE = r"(?![^\W_]|[*_])"
_BOLD_STAR_RE = re.compile(_OPEN + r"\*\*(\S(?:.*?\S)?)\*\*" + _CLOSE)
_BOLD_UNDERSCORE_RE = re.compile(_OPEN + r"__(\S(?:.*?\S)?)__" + _CLOSE)
_EM_STAR_RE = re.compile(_OPEN + r"\*(\S(?:.*?\S)?)\*" + _CLOSE)
_EM_UNDERSCORE_RE = re.compile(_OPEN + r"_(\S(?:.*?\S)?)_" + _CLOSE)


def _convert_line(line: str) -> str:
    heading_match = _HEADING_RE.match(line)
    if heading_match:
        line = heading_match.group(1)

    list_match = _LIST_MARKER_RE.match(line)
    if list_match:
        indent, rest = list_match.groups()
        line = f"{indent}— {rest}"

    # Bold patterns must run before the single-marker ones, or "**bold**"
    # gets misread as a single-marker pair on its inner two asterisks.
    line = _BOLD_STAR_RE.sub(r"\1", line)
    line = _BOLD_UNDERSCORE_RE.sub(r"\1", line)
    line = _EM_STAR_RE.sub(r"\1", line)
    line = _EM_UNDERSCORE_RE.sub(r"\1", line)

    return line.replace("`", "")


def to_plain_text(text: str) -> str:
    return "\n".join(_convert_line(line) for line in text.split("\n"))
