"""A clickable map link for a coordinate.

Its own module because two things need it and neither should own it: the
status report renders it, and whatever records a shared pin decides there is
one to render.

A bare URL, not Markdown. `parse_mode` is never set anywhere in bot/ (see
bot/formatting.py) — every message goes out as plain text, because Telegram
drops a whole message on unbalanced entities and item names come from users.
Telegram auto-links a bare https:// URL, so a link costs nothing and risks
nothing, while "[Место](url)" would arrive as literal brackets.
"""

import re

# Google Maps rather than a geo: URI or an OpenStreetMap link: geo: opens
# nothing on desktop, and this form is what every phone's map app already
# handles.
_TEMPLATE = "https://www.google.com/maps?q={lat},{lon}"

# Six decimals is about 0.1 m. More is noise that only makes the URL longer,
# and the coordinates arrive from Telegram with more than anyone needs.
_PRECISION = 6


def maps_link(lat, lon) -> str | None:
    """A maps URL, or None when either coordinate is missing.

    Returns None rather than raising or building a broken URL: a place with
    no coordinates is the ordinary case, not an error, and every caller has
    to handle it anyway.
    """
    if lat is None or lon is None:
        return None
    try:
        return _TEMPLATE.format(lat=round(float(lat), _PRECISION),
                                lon=round(float(lon), _PRECISION))
    except (TypeError, ValueError):
        return None


# A coordinate pair anywhere in a place name, with or without a word
# introducing it. Live, the model recorded the place as
# "Бен шемен, координаты 31.9460200, 34.9434050": the name and the
# coordinates in one string.
#
# That is the same mistake as an item called "2 кг мяса" — data stuffed into
# a name field — and it costs the same two things. The report reads like a
# database row, and the name no longer matches anything in `places`, so the
# coordinates that would have made a link are unreachable.
_COORDINATES = re.compile(
    r"[\s,;:—–-]*"
    # An opening bracket, when the pair is parenthesised.
    r"[(\[]?"
    r"\s*(?:коорд\w*|coord\w*|gps|geo|широта|долгота|lat\w*|lon\w*)?"
    r"[\s,;:.]*"
    r"-?\d{1,3}\.\d{3,}\s*[,;]?\s*-?\d{1,3}\.\d{3,}"
    r"\s*[)\]]?[\s,;.]*$",
    re.IGNORECASE,
)


def strip_coordinates(name: str) -> str:
    """A place name with a trailing coordinate pair removed.

    Only at the end, and only with at least three decimals on both numbers:
    that is what a machine writes and what a person never does. A name that
    is *nothing but* coordinates is left alone — bot/router.py stores a pin
    that way on purpose when nobody has named the place yet, and stripping it
    would leave an empty name.
    """
    if not isinstance(name, str) or not name.strip():
        return name
    stripped = _COORDINATES.sub("", name).strip(" ,;:—–-")
    return stripped or name.strip()


# Hosts that only ever mean "here is a place". Matched on the host, not on
# the path, because every one of these has several link shapes — a short
# link, a share link, a coordinate link — and enumerating them ages badly.
_NAVIGATOR_HOSTS = (
    "google.com/maps", "google.co", "maps.google.", "maps.app.goo.gl", "goo.gl/maps",
    "waze.com", "waze.to", "ul.waze.com",
    "maps.apple.com", "maps.yandex.", "yandex.com/maps", "yandex.ru/maps",
    "2gis.", "openstreetmap.org", "osm.org", "w3w.co", "what3words.com",
    "moovitapp.com", "here.com", "mapy.cz", "komoot.", "organicmaps.app",
)

_URL = re.compile(r"https?://\S+", re.IGNORECASE)

# A coordinate pair anywhere in a URL. The catch-all for a navigator nobody
# listed above: a link carrying a latitude and a longitude is a link to a
# place, whoever generated it.
_URL_COORDINATES = re.compile(r"-?\d{1,3}\.\d{3,}[,/;+]-?\d{1,3}\.\d{3,}")


def find_map_url(text: str) -> str | None:
    """The first navigator link in a message, or None.

    Recognised by host, plus any URL carrying a coordinate pair. Deliberately
    not resolved, not expanded and not checked: a short link that maps refuse
    to expand is still a link that opens correctly on the phone of whoever
    receives it, and the bot answering "не смог открыть эту короткую ссылку"
    was worse than useless — it turned a working link into a conversation.

    Trailing punctuation is dropped: "вот сюда: https://waze.com/ul/x." would
    otherwise store a URL with a full stop welded on.
    """
    if not isinstance(text, str):
        return None
    for match in _URL.finditer(text):
        url = match.group().rstrip(".,;:!?)]}»\"'")
        lowered = url.lower()
        if any(host in lowered for host in _NAVIGATOR_HOSTS):
            return url
        if _URL_COORDINATES.search(url):
            return url
    return None
