from unittest.mock import AsyncMock, patch

import worker.main as worker_main


async def test_poll_once_calls_all_three_worker_steps():
    pool, telegram_bot = object(), object()

    with (
        patch("worker.main.deliver_due_reminders", AsyncMock()) as deliver,
        patch("worker.main.fire_closing_questions", AsyncMock()) as closing_q,
        patch("worker.main.fire_auto_closes", AsyncMock()) as auto_close,
    ):
        await worker_main.poll_once(pool, telegram_bot, min_interval_hours=6)

    deliver.assert_awaited_once_with(pool, telegram_bot, min_interval_hours=6)
    closing_q.assert_awaited_once_with(pool, telegram_bot)
    auto_close.assert_awaited_once_with(pool, telegram_bot)
