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
