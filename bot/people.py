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


def humans(member_count: int | None) -> int | None:
    """How many people are in a chat, with the bot itself taken out.

    getChatMemberCount counts the bot as a member, but every sentence the bot
    writes is about the people it is organizing — "В группе 5 человек" that
    silently includes itself is wrong in a way the reader cannot check.

    None stays None rather than becoming 0: never having fetched the count is
    not the same as the group being empty, and the one thing the bot must
    never claim is knowing who is in a chat when it does not.
    """
    if member_count is None:
        return None
    return max(0, member_count - 1)


def how_many(count: int) -> str:
    """The count with its noun: 1 человек, 2 человека, 5 человек.

    Russian agreement, in one place, because the bot says this in the
    greeting, the roll-call summary and the status report. 11-14 take the
    same form as 5+ despite ending in 1-4, which is the case a naive
    `count == 1` check gets wrong.
    """
    tail, last = count % 100, count % 10
    plural = last in (2, 3, 4) and not 11 <= tail <= 14
    return f"{count} {'человека' if plural else 'человек'}"
