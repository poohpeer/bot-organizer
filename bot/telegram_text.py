"""Sending a message that may contain a link, without letting HTML loose.

`parse_mode` is deliberately never set anywhere else (see bot/formatting.py):
Telegram drops a whole message on unbalanced entities, and item names, place
names and the model's own words all come from users. One stray "<" would lose
the reply entirely.

A link needs entities, though — "🔗 Map" pointing somewhere cannot be plain
text. So links are written by renderers as a marker, and this module is the
only thing that turns a marker into HTML. Two properties keep that safe:

  * a message with no marker is sent exactly as it is today, plain, with no
    parse_mode at all — so nothing that works now can start failing;
  * a message with one is escaped in full first, and the only tags that
    survive are the anchors this module writes itself, so entities are
    balanced by construction rather than by hope.

The marker uses private-use characters: no user types them, no Markdown
converter touches them, and bot/formatting.py passes them through unchanged.
"""

import html
import re

_OPEN = ""
_SEP = ""
_CLOSE = ""

_MARKER = re.compile(f"{_OPEN}(.*?){_SEP}(.*?){_CLOSE}", re.DOTALL)

# Only these can be a link target. A marker is written by our own code today,
# but the URL inside one comes from a place someone shared, and "javascript:"
# in an href is the reason this list is a list rather than a comment.
_ALLOWED_SCHEMES = ("https://", "http://")


def link(url: str, label: str) -> str:
    """A marker for `label` pointing at `url`, to be embedded in text.

    Left as plain `label` when the URL is not one we would follow, so a
    caller never has to check: the text still reads correctly, it simply
    is not clickable.
    """
    if not isinstance(url, str) or not url.startswith(_ALLOWED_SCHEMES):
        return label
    return f"{_OPEN}{url}{_SEP}{label}{_CLOSE}"


def has_link(text: str) -> bool:
    return isinstance(text, str) and _MARKER.search(text) is not None


def to_html(text: str) -> str:
    """Escaped text with markers turned into anchors.

    Escaping happens first and over everything, so a place called "<b>" is
    shown, not interpreted, and cannot leave an entity open.
    """
    escaped = html.escape(text)
    # The markers survive escaping untouched — they are private-use
    # characters, which html.escape has no opinion about.
    return _MARKER.sub(
        lambda m: f'<a href="{html.unescape(m.group(1))}">{m.group(2)}</a>', escaped
    )


def strip_links(text: str) -> str:
    """The same text with markers reduced to their labels.

    For anywhere a message can go out without this module — a log line, a
    reminder body, decision_log. A marker that reaches a person as private-use
    control characters is worse than a link that was never there.
    """
    if not isinstance(text, str):
        return text
    return _MARKER.sub(lambda m: m.group(2), text)


# Telegram refuses a message longer than this with 400 "message is too long".
# Nothing caught that: the exception left the handler, the group saw nothing,
# and the only trace was a line in the log — the same shape as a bot that
# quietly says nothing at all.
MAX_MESSAGE_CHARS = 4096

# Measured on the escaped text, since "&" becomes "&amp;" on the way out and
# a report full of ampersands would otherwise sail past the check and be
# refused anyway. A little headroom rather than exactly 4096: an entity
# straddling the boundary is worth avoiding, and nobody misses 96 characters.
_SPLIT_AT = MAX_MESSAGE_CHARS - 96


def split_for_telegram(text: str, limit: int = _SPLIT_AT) -> list[str]:
    """One message per chunk, cut at line boundaries.

    Lines are the seam because everything long here is a list: the shopping
    list, the roster, the status report. A cut mid-line splits an item in
    half, and — with HTML — could split an entity, which loses the whole
    message rather than making it ugly. Every anchor this module writes lives
    inside one line, so a line boundary is always safe.

    A single line longer than the limit is cut by characters. That case needs
    somebody to have typed a 4000-character item name; the alternative is
    refusing to send anything at all.

    Truncation is not on offer. A list exists to be complete, and a report
    that silently stops halfway is worse than two messages.
    """
    if len(text) <= limit:
        return [text]

    chunks, current = [], ""
    for line in text.split("\n"):
        while len(line) > limit:
            if current:
                chunks.append(current)
                current = ""
            chunks.append(line[:limit])
            line = line[limit:]
        candidate = f"{current}\n{line}" if current else line
        if len(candidate) > limit:
            chunks.append(current)
            current = line
        else:
            current = candidate
    if current:
        chunks.append(current)
    return chunks


async def send_text(telegram_bot, chat_id: int, text: str, **kwargs):
    """send_message, with HTML only when the text actually needs it.

    A message carrying a link also gets its preview turned off. Telegram
    expands the first URL it finds into a card, and for a maps link that card
    is a picture of the map with the coordinates as its title — half a phone
    screen of it, pushed under a status report whose whole point is to be
    read at a glance. The link is already a link; the card adds a second,
    larger copy of it.

    Too long for one message and it goes as several, in order. Returns the
    last one sent, which is what a single-message caller expects back.
    """
    html = has_link(text)
    body = to_html(text) if html else text
    extra = {"parse_mode": "HTML", "disable_web_page_preview": True} if html else {}

    sent = None
    for chunk in split_for_telegram(body):
        sent = await telegram_bot.send_message(
            chat_id=chat_id, text=chunk, **extra, **kwargs
        )
    return sent
