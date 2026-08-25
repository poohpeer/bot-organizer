import logging

import db.pool as db_pool_module
from bot.logging_setup import configure_logging, truncate


def test_truncate_keeps_short_values_intact():
    assert truncate("короткая строка") == "короткая строка"


def test_truncate_collapses_whitespace_and_caps_length():
    """SQL and prompts arrive formatted across many lines; a log line must stay
    a line."""
    result = truncate("a" * 500)

    assert result.endswith("(+200 chars)")
    assert "\n" not in truncate("SELECT *\n  FROM chats\n  WHERE id = 1")


def test_third_party_loggers_are_muzzled():
    """The whole anti-flood strategy: DEBUG for our packages, not for httpx and
    telegram.request, which log a line per HTTP call and per poll cycle."""
    configure_logging()

    assert logging.getLogger("bot").level == logging.DEBUG
    assert logging.getLogger("worker").level == logging.DEBUG
    assert logging.getLogger("httpx").level == logging.WARNING
    assert logging.getLogger("telegram.request").level == logging.INFO
    assert logging.getLogger("google_genai").level == logging.INFO


def test_pool_housekeeping_is_not_logged():
    """asyncpg resets the session on every release and our setup hook re-pins
    the schema on every acquire. Logging those wraps every real query in two
    lines of noise."""
    assert db_pool_module._is_housekeeping("SET search_path TO test_abc")
    assert db_pool_module._is_housekeeping(
        "SELECT pg_advisory_unlock_all(); CLOSE ALL; UNLISTEN *; RESET ALL;"
    )
    assert not db_pool_module._is_housekeeping("SELECT * FROM sessions WHERE id = $1")


def test_sql_summary_strips_comments_and_shortens():
    """Our SQL carries long explanatory comments; they must not reach the log."""
    summary = db_pool_module._sql_summary(
        """
        -- a long explanation of why this query exists at all
        SELECT id FROM sessions WHERE chat_id = $1
        """
    )

    assert summary.startswith("SELECT id FROM sessions")
    assert "explanation" not in summary


async def test_maps_lookup_logs_the_url_but_never_the_api_key(db_pool, caplog):
    """Logging "all external requests" is one careless line away from putting
    the API key in every log file."""
    import bot.tools.external as external

    with caplog.at_level(logging.DEBUG, logger="bot.tools.external"):
        await external.maps_lookup("nowhere at all zzz")

    logged = "\n".join(r.getMessage() for r in caplog.records)
    assert "places:searchText" in logged
    assert external._MAPS_API_KEY not in logged
