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

# Emphasis markers are only ever removed in pairs found on the same line, and
# only when neither side has whitespace touching the marker. That second
# condition is what keeps "2 * 3 = 6" from being read as an opening/closing
# pair: a real emphasis run never has a space right after "*" or right before
# the closing "*". A single, unpaired marker (no partner at all on the line,
# e.g. "звёздочка*") never matches these patterns and survives untouched —
# that's the R3 case, and the reason this isn't `text.replace("*", "")`.
_BOLD_STAR_RE = re.compile(r"\*\*(\S(?:.*?\S)?)\*\*")
_BOLD_UNDERSCORE_RE = re.compile(r"__(\S(?:.*?\S)?)__")
_EM_STAR_RE = re.compile(r"\*(\S(?:.*?\S)?)\*")
_EM_UNDERSCORE_RE = re.compile(r"_(\S(?:.*?\S)?)_")


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
