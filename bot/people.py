"""How a person is named in anything the group reads.

One function, used by every renderer, so the same person reads the same way in
the roster, the shopping list, the roll call and the status report. Splitting
this across renderers is how "Alex" in one block and "@poohpeer" in another
came to look like two people.
"""


def mention(display_name: str | None, username: str | None = None) -> str:
    """The @username when one is known, otherwise the name as given.

    The '@' is not decoration: Telegram turns "@name" in message text into a
    live mention, so the person actually gets a notification. A display name
    notifies nobody, and two Sashas in one chat are indistinguishable by it.

    A name is never turned into a username — "@" is only ever written in front
    of something Telegram gave us. Guessing one would produce a mention of
    whoever really owns that handle.
    """
    if username:
        return "@" + username.lstrip("@")
    return display_name or "?"


def normalise_username(raw: str | None) -> str | None:
    """Strip the leading '@' and blank-to-None, so lookups match.

    Usernames reach us three ways — from `from_user.username` (no '@'), from a
    model that copied "@poohpeer" out of a message, and from a person typing
    either — and the unique index is on lower(username). Storing both forms
    would let the same handle occupy two rows, which is the duplicate this
    whole column exists to prevent.
    """
    if raw is None:
        return None
    cleaned = raw.strip().lstrip("@").strip()
    return cleaned or None
