"""The worker's third duty (see worker.main.poll_once): notice when a group's
own title/description change, and say so once — never once per poll.

Only chats with an active session are touched at all. A dormant chat is not
being tracked (R5 from 0001), so even fetching its info would be the bot
watching a chat nobody asked it to watch; skipping it is done in the SQL
below, before a single Telegram call is made.
"""

import logging
import os
from datetime import date

import bot.group_info as group_info
import bot.timezones as timezones
import bot.tools.core as core_tools

log = logging.getLogger(__name__)

# The worker itself polls every 60s; without this, syncing would mean one
# get_chat per chat with an active session, every single poll, forever.
# Gated via chats.info_checked_at rather than a separate timer, so a chat
# that has never been checked (NULL) is always eligible regardless of when
# the worker last ran.
GROUP_SYNC_INTERVAL_SECONDS = int(os.environ.get("GROUP_SYNC_INTERVAL_SECONDS", "60"))


def _parse_iso_date(value) -> date | None:
    # Mirrors bot.router._parse_event_date: the classifier is asked for
    # ISO-8601 but isn't guaranteed to comply, and a malformed date here must
    # not crash the whole sync pass for every other chat.
    if not isinstance(value, str):
        return None
    try:
        return date.fromisoformat(value)
    except ValueError:
        return None


def _describe_change(changes: dict, info: dict) -> str:
    sentences = []
    if changes["title_changed"]:
        sentences.append(f"Название чата теперь «{info.get('title') or ''}».")
    if changes["description_changed"]:
        sentences.append(f"Описание чата теперь: «{info.get('description') or ''}».")
    return " ".join(sentences)


async def sync_group_info(pool, telegram_bot) -> dict:
    """Check every chat with an active session for a title/description
    change, announce each one exactly once, and refresh chats.member_count
    along the way.

    Each chat is isolated in its own try/except, the same discipline
    worker.closing follows: one chat the bot has been kicked from must not
    strand a poll that would otherwise have announced changes for every
    other chat behind it.
    """
    rows = await pool.fetch(
        """
        SELECT s.id AS session_id, s.chat_id
        FROM sessions s JOIN chats c USING (chat_id)
        WHERE s.status = 'active'
          AND (c.info_checked_at IS NULL
               OR c.info_checked_at <= now() - $1 * interval '1 second')
        """,
        GROUP_SYNC_INTERVAL_SECONDS,
    )

    announced = []
    for row in rows:
        chat_id, session_id = row["chat_id"], row["session_id"]
        try:
            info = await group_info.fetch(telegram_bot, chat_id)
            if not info:
                # Unreachable this pass. info_checked_at is left untouched —
                # not updated to "now, but empty" — so the interval gate lets
                # the very next poll try again instead of waiting out a full
                # interval for a chat that was never actually checked.
                continue

            if info.get("member_count") is not None:
                await pool.execute(
                    "UPDATE chats SET member_count = $2 WHERE chat_id = $1",
                    chat_id, info["member_count"],
                )

            # Compares against, and stores, the new values in one call — the
            # single most important line in this function. Splitting compare
            # and store into two steps is what would turn one real change
            # into the same announcement on every poll thereafter.
            changes = await group_info.changes_since_last_seen(pool, chat_id, info)
            if not (changes["title_changed"] or changes["description_changed"]):
                continue

            tz = await timezones.chat_timezone(pool, chat_id)
            event = await group_info.extract_event(
                info.get("title"), info.get("description"), timezones.local_date(tz)
            )
            parsed_date = _parse_iso_date(event.get("event_date"))
            if parsed_date is not None:
                await pool.execute(
                    "UPDATE sessions SET event_date = $2 WHERE id = $1", session_id, parsed_date
                )
            if event.get("place"):
                await core_tools.remember_fact(pool, session_id, "place", event["place"])

            await telegram_bot.send_message(chat_id=chat_id, text=_describe_change(changes, info))
            announced.append(chat_id)
        except Exception:
            log.warning("Group sync failed for chat %s", chat_id, exc_info=True)

    return {"announced": announced}
