"""The worker's third duty (see worker.main.poll_once): notice when a group's
own title/description change, and bring the event into line with them.

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

log = logging.getLogger(__name__)

# The worker itself polls every 60s; without this, syncing would mean one
# get_chat per chat with an active session, every single poll, forever.
# Gated via chats.info_checked_at rather than a separate timer, so a chat
# that has never been checked (NULL) is always eligible regardless of when
# the worker last ran.
GROUP_SYNC_INTERVAL_SECONDS = int(os.environ.get("GROUP_SYNC_INTERVAL_SECONDS", "60"))


async def sync_group_info(pool, telegram_bot) -> dict:
    """Check every chat with an active session for a title/description
    change, apply whatever it says about the event, and refresh
    chats.member_count along the way.

    Nothing is posted to the chat. A group renaming its own chat can see
    that it did; being told about it is noise, and the announcement was
    landing even when the extraction behind it had failed and nothing was
    actually applied.

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

    updated = []
    for row in rows:
        chat_id, session_id = row["chat_id"], row["session_id"]
        try:
            # Before anything that needs the chat's zone: the creator's own
            # zone is one of the things that decides it.
            await group_info.ensure_creator_known(pool, telegram_bot, chat_id)
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

            # Read before the change is recorded as seen, so a failed
            # extraction leaves the change unconsumed and the next pass tries
            # again. It used to be the other way round: the title was stored
            # as seen, the classifier chain then 400ed, and the change was
            # gone forever — live, "Маленькая прага 31/8" was announced to
            # the chat and never reached the event at all.
            changed = await group_info.changed_fields(pool, chat_id, info)
            if not changed:
                # A first sighting lands here too, and still has to be
                # recorded — that is what turns comparison on for this chat.
                await group_info.record_as_seen(pool, chat_id, info)
                continue

            tz = await timezones.chat_timezone(pool, chat_id)
            event = await group_info.extract_event(
                info.get("title"), info.get("description"), timezones.local_date(tz)
            )
            if not event["answered"]:
                # Nobody in the chain could read it. Leaving the change
                # unrecorded is the whole point: the next pass tries again
                # instead of the new title being lost because one classifier
                # call happened to fail.
                log.warning("Chat %s changed %s but the classifier did not answer; "
                            "leaving it for the next pass", chat_id, sorted(changed))
                continue

            applied = await group_info.apply_event(pool, session_id, event)
            # After the work, not before. A title that simply says nothing
            # about an event is recorded too, so a chat called "Друзья" is
            # not re-read every minute forever.
            await group_info.record_as_seen(pool, chat_id, info)
            if applied:
                log.info("Chat %s: applied %s from its own title/description",
                         chat_id, applied)
                updated.append({"chat_id": chat_id, **applied})
        except Exception:
            log.warning("Group sync failed for chat %s", chat_id, exc_info=True)

    return {"updated": updated}
