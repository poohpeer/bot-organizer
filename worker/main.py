import asyncio
import functools
import logging
import os

from telegram import Bot

import db.pool as db_pool_module
from bot.logging_setup import configure_logging
from worker.closing import fire_auto_closes, fire_closing_questions
from worker.group_sync import sync_group_info
from worker.reminders import deliver_due_reminders

configure_logging()
log = logging.getLogger(__name__)

POLL_INTERVAL_SECONDS = 60


def _did_something(result) -> bool:
    """True when a step actually acted. Steps return either a list of ids or a
    dict of id lists; both are empty on an idle poll."""
    if isinstance(result, dict):
        return any(result.values())
    return bool(result)


async def poll_once(pool, telegram_bot, *, min_interval_hours: float) -> None:
    """Run one pass of every scheduled duty.

    The steps are independent, so each is isolated: a database hiccup while
    delivering reminders must not stop closing questions from being asked for
    the rest of the day. Without this the first step to fail silently disables
    the two behind it.
    """
    steps = (
        ("reminders", functools.partial(
            deliver_due_reminders, pool, telegram_bot, min_interval_hours=min_interval_hours)),
        ("closing questions", functools.partial(fire_closing_questions, pool, telegram_bot)),
        ("auto-closes", functools.partial(fire_auto_closes, pool, telegram_bot)),
        ("group sync", functools.partial(sync_group_info, pool, telegram_bot)),
    )
    for name, step in steps:
        try:
            result = await step()
        except Exception:
            log.exception("Worker step %r failed this poll", name)
            continue
        # A poll that found nothing is the overwhelmingly common case — 1440 of
        # them a day. Logging those would drown the ones that did something.
        # Filtered here rather than by making the steps return nothing, which
        # would change their contract for a logging convenience.
        if _did_something(result):
            log.info("Worker %s: %s", name, result)


async def main() -> None:
    pool = await db_pool_module.create_pool(os.environ["DATABASE_URL"])
    await db_pool_module.init_db(pool)
    telegram_bot = Bot(token=os.environ["BOT_ORGANIZER_BOT_TOKEN"])
    min_interval_hours = float(os.environ.get("REMINDER_MIN_INTERVAL_HOURS", "6"))

    log.info("Worker started, polling every %ds", POLL_INTERVAL_SECONDS)
    while True:
        try:
            await poll_once(pool, telegram_bot, min_interval_hours=min_interval_hours)
        except Exception:
            log.exception("Poll iteration failed, continuing")
        await asyncio.sleep(POLL_INTERVAL_SECONDS)


if __name__ == "__main__":
    asyncio.run(main())
