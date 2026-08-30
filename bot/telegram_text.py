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


async def send_text(telegram_bot, chat_id: int, text: str, **kwargs):
    """send_message, with HTML only when the text actually needs it.

    A message carrying a link also gets its preview turned off. Telegram
    expands the first URL it finds into a card, and for a maps link that card
    is a picture of the map with the coordinates as its title — half a phone
    screen of it, pushed under a status report whose whole point is to be
    read at a glance. The link is already a link; the card adds a second,
    larger copy of it.
    """
    if has_link(text):
        return await telegram_bot.send_message(
            chat_id=chat_id, text=to_html(text), parse_mode="HTML",
            disable_web_page_preview=True, **kwargs
        )
    return await telegram_bot.send_message(chat_id=chat_id, text=text, **kwargs)
