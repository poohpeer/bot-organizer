from unittest.mock import AsyncMock

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
