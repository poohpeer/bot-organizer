import asyncio
from unittest.mock import AsyncMock

import pytest

import bot.proactive as proactive


def test_match_topic_finds_a_keyword_category():
    assert proactive.match_topic("давно мы не были на пикнике") == "havent_in_a_while"
    assert proactive.match_topic("может, на выходных махнём куда-то") == "should_go_somewhere"
    assert proactive.match_topic("какая сегодня погода") is None


async def test_maybe_suggest_skips_when_no_keyword_match(db_pool):
    result = await proactive.maybe_suggest(db_pool, AsyncMock(), chat_id=1, text="какая сегодня погода")

    assert result == {"suggested": False, "reason": "no_keyword_match"}


async def test_maybe_suggest_posts_when_classifier_confirms(monkeypatch, db_pool):
    monkeypatch.setattr(proactive, "classify", AsyncMock(return_value=True))
    telegram_bot = AsyncMock()

    result = await proactive.maybe_suggest(
        db_pool, telegram_bot, chat_id=1, text="давно мы не собирались все вместе"
    )

    assert result["suggested"] is True
    telegram_bot.send_message.assert_awaited_once()
    row = await db_pool.fetchrow("SELECT * FROM proactive_suggestions WHERE id = $1", result["suggestion_id"])
    assert row["topic_key"] == "havent_in_a_while"
    assert row["response"] is None


async def test_maybe_suggest_stays_silent_when_classifier_says_not_a_lead(monkeypatch, db_pool):
    monkeypatch.setattr(proactive, "classify", AsyncMock(return_value=False))
    telegram_bot = AsyncMock()

    result = await proactive.maybe_suggest(
        db_pool, telegram_bot, chat_id=1, text="давно мы не виделись с дядей Колей, земля ему пухом"
    )

    assert result == {"suggested": False, "reason": "not_a_lead"}
    telegram_bot.send_message.assert_not_awaited()


async def test_maybe_suggest_rate_limited_within_seven_days(monkeypatch, db_pool):
    monkeypatch.setattr(proactive, "classify", AsyncMock(return_value=True))
    await db_pool.execute(
        "INSERT INTO proactive_suggestions (chat_id, topic_key) VALUES (1, 'lets_go')"
    )

    result = await proactive.maybe_suggest(
        db_pool, AsyncMock(), chat_id=1, text="давно мы не собирались"
    )

    assert result == {"suggested": False, "reason": "rate_limited"}


async def test_maybe_suggest_topic_suppressed_after_decline(monkeypatch, db_pool):
    monkeypatch.setattr(proactive, "classify", AsyncMock(return_value=True))
    await db_pool.execute(
        """
        INSERT INTO proactive_suggestions (chat_id, topic_key, suggested_at, response)
        VALUES (1, 'havent_in_a_while', now() - interval '10 days', 'declined')
        """
    )

    result = await proactive.maybe_suggest(
        db_pool, AsyncMock(), chat_id=1, text="давно мы не собирались все вместе"
    )

    assert result == {"suggested": False, "reason": "topic_suppressed"}


async def test_get_pending_suggestion_returns_unresolved_one(db_pool):
    row = await db_pool.fetchrow(
        "INSERT INTO proactive_suggestions (chat_id, topic_key) VALUES (1, 'lets_go') RETURNING id"
    )

    found = await proactive.get_pending_suggestion(db_pool, chat_id=1)

    assert found["id"] == row["id"]


async def test_get_pending_suggestion_none_when_already_resolved(db_pool):
    await db_pool.execute(
        "INSERT INTO proactive_suggestions (chat_id, topic_key, response) VALUES (1, 'lets_go', 'accepted')"
    )

    assert await proactive.get_pending_suggestion(db_pool, chat_id=1) is None


async def test_resolve_suggestion_sets_response(db_pool):
    row = await db_pool.fetchrow(
        "INSERT INTO proactive_suggestions (chat_id, topic_key) VALUES (1, 'lets_go') RETURNING id"
    )

    await proactive.resolve_suggestion(db_pool, row["id"], "declined")

    updated = await db_pool.fetchrow("SELECT response FROM proactive_suggestions WHERE id = $1", row["id"])
    assert updated["response"] == "declined"


async def test_maybe_suggest_enforces_the_weekly_limit_under_concurrency(monkeypatch, db_pool):
    """R5 puts the rate limit in code, not in a prompt. Two messages arriving
    together both cleared the advisory pre-check and both suggested, so a chat
    could get two proactive pings in one week."""
    monkeypatch.setattr(proactive, "classify", AsyncMock(return_value=True))
    telegram_bot = AsyncMock()

    results = await asyncio.gather(*(
        proactive.maybe_suggest(db_pool, telegram_bot, chat_id=1, text=text)
        for text in ("давно мы не собирались все вместе", "давно мы не виделись, надо исправить")
    ))

    assert [r["suggested"] for r in results].count(True) == 1
    assert telegram_bot.send_message.await_count == 1
    assert await db_pool.fetchval("SELECT count(*) FROM proactive_suggestions WHERE chat_id = 1") == 1


async def test_maybe_suggest_withdraws_the_claim_when_telegram_refuses(monkeypatch, db_pool):
    """A suggestion nobody saw must not silence the chat for a week, and must
    not sit pending so the next unrelated message reads as a reply to it."""
    monkeypatch.setattr(proactive, "classify", AsyncMock(return_value=True))
    telegram_bot = AsyncMock()
    telegram_bot.send_message.side_effect = RuntimeError("Forbidden: bot was blocked by the user")

    result = await proactive.maybe_suggest(
        db_pool, telegram_bot, chat_id=1, text="давно мы не собирались все вместе"
    )

    assert result == {"suggested": False, "reason": "send_failed"}
    assert await db_pool.fetchval("SELECT count(*) FROM proactive_suggestions WHERE chat_id = 1") == 0
    assert await proactive.get_pending_suggestion(db_pool, chat_id=1) is None


async def test_maybe_suggest_does_not_break_the_chat_when_telegram_refuses(monkeypatch, db_pool):
    """S9 calls this for every message in a dormant chat, so a failed
    suggestion must not propagate and take ordinary handling down with it."""
    monkeypatch.setattr(proactive, "classify", AsyncMock(return_value=True))
    telegram_bot = AsyncMock()
    telegram_bot.send_message.side_effect = RuntimeError("network is down")

    result = await proactive.maybe_suggest(
        db_pool, telegram_bot, chat_id=1, text="давно мы не собирались все вместе"
    )

    assert result["suggested"] is False


async def test_maybe_suggest_makes_no_model_call_when_rate_limited(monkeypatch, db_pool):
    """R5: "the rate limit is enforced in code before any model call"."""
    classifier = AsyncMock(return_value=True)
    monkeypatch.setattr(proactive, "classify", classifier)
    await db_pool.execute("INSERT INTO proactive_suggestions (chat_id, topic_key) VALUES (1, 'lets_go')")

    result = await proactive.maybe_suggest(
        db_pool, AsyncMock(), chat_id=1, text="давно мы не собирались"
    )

    assert result == {"suggested": False, "reason": "rate_limited"}
    classifier.assert_not_awaited()


async def test_maybe_suggest_makes_no_model_call_when_topic_suppressed(monkeypatch, db_pool):
    classifier = AsyncMock(return_value=True)
    monkeypatch.setattr(proactive, "classify", classifier)
    await db_pool.execute(
        """
        INSERT INTO proactive_suggestions (chat_id, topic_key, suggested_at, response)
        VALUES (1, 'havent_in_a_while', now() - interval '10 days', 'declined')
        """
    )

    await proactive.maybe_suggest(db_pool, AsyncMock(), chat_id=1, text="давно мы не собирались все вместе")

    classifier.assert_not_awaited()


async def test_resolve_suggestion_rejects_a_value_the_schema_forbids(db_pool):
    """Guards the router: an unexpected value used to surface as an asyncpg
    CheckViolationError from deep in the call stack."""
    row = await db_pool.fetchrow(
        "INSERT INTO proactive_suggestions (chat_id, topic_key) VALUES (1, 'lets_go') RETURNING id"
    )

    with pytest.raises(ValueError):
        await proactive.resolve_suggestion(db_pool, row["id"], "maybe")

    unchanged = await db_pool.fetchrow(
        "SELECT response FROM proactive_suggestions WHERE id = $1", row["id"]
    )
    assert unchanged["response"] is None


async def test_an_accepted_topic_is_not_suppressed(monkeypatch, db_pool):
    """R5 suppresses a topic for 30 days when a suggestion is ignored or
    declined — accepting it must not lock the topic out."""
    monkeypatch.setattr(proactive, "classify", AsyncMock(return_value=True))
    await db_pool.execute(
        """
        INSERT INTO proactive_suggestions (chat_id, topic_key, suggested_at, response)
        VALUES (1, 'havent_in_a_while', now() - interval '10 days', 'accepted')
        """
    )

    result = await proactive.maybe_suggest(
        db_pool, AsyncMock(), chat_id=1, text="давно мы не собирались все вместе"
    )

    assert result["suggested"] is True
