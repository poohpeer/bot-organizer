from unittest.mock import AsyncMock, MagicMock, patch

import worker.main as worker_main


async def test_poll_once_calls_all_four_worker_steps():
    pool, telegram_bot = object(), object()

    with (
        patch("worker.main.deliver_due_reminders", AsyncMock()) as deliver,
        patch("worker.main.fire_closing_questions", AsyncMock()) as closing_q,
        patch("worker.main.fire_auto_closes", AsyncMock()) as auto_close,
        patch("worker.main.sync_group_info", AsyncMock()) as group_sync,
    ):
        await worker_main.poll_once(pool, telegram_bot, min_interval_hours=6)

    deliver.assert_awaited_once_with(pool, telegram_bot, min_interval_hours=6)
    closing_q.assert_awaited_once_with(pool, telegram_bot)
    auto_close.assert_awaited_once_with(pool, telegram_bot)
    group_sync.assert_awaited_once_with(pool, telegram_bot)


async def test_one_failing_step_does_not_block_the_others(monkeypatch):
    """The steps are independent duties. A database hiccup while delivering
    reminders must not stop the day's closing questions from being asked."""
    pool, telegram_bot = MagicMock(name="pool"), AsyncMock()
    monkeypatch.setattr(worker_main, "deliver_due_reminders",
                        AsyncMock(side_effect=RuntimeError("connection reset")))
    closing = AsyncMock()
    auto_close = AsyncMock()
    group_sync = AsyncMock()
    monkeypatch.setattr(worker_main, "fire_closing_questions", closing)
    monkeypatch.setattr(worker_main, "fire_auto_closes", auto_close)
    monkeypatch.setattr(worker_main, "sync_group_info", group_sync)

    await worker_main.poll_once(pool, telegram_bot, min_interval_hours=6)

    closing.assert_awaited_once_with(pool, telegram_bot)
    auto_close.assert_awaited_once_with(pool, telegram_bot)
    group_sync.assert_awaited_once_with(pool, telegram_bot)
