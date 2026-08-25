"""Logging configuration shared by both processes.

The default is DEBUG for our own code and INFO for everything else. That
distinction is the whole anti-flood strategy: httpx logs a line per request,
`telegram.ext` narrates every poll cycle, and `google_genai` is chattier still.
Turning the root logger to DEBUG without muzzling them buries the handful of
lines that actually explain what the bot decided.
"""

import logging
import os

# Third-party loggers that are useful at INFO and unusable at DEBUG.
_NOISY = {
    "httpx": logging.WARNING,          # one line per outbound request; ours are better
    "httpcore": logging.WARNING,       # connection-pool internals
    "telegram.ext.Updater": logging.INFO,
    "telegram.ext.Application": logging.INFO,
    "telegram.request": logging.INFO,  # DEBUG dumps every getUpdates payload
    "google_genai": logging.INFO,
    "google_genai.models": logging.INFO,
    "asyncio": logging.INFO,
}

_OUR_PACKAGES = ("bot", "worker", "db")

MAX_LOGGED_CHARS = int(os.environ.get("LOG_MAX_CHARS", "300"))


def truncate(value, limit: int | None = None) -> str:
    """Render a value for a log line without letting it become the log line.

    Model prompts, search results and SQL are all things you want to see the
    shape of and never want in full, thousands of times a day.
    """
    limit = MAX_LOGGED_CHARS if limit is None else limit
    text = value if isinstance(value, str) else repr(value)
    text = " ".join(text.split())
    if len(text) <= limit:
        return text
    return f"{text[:limit]}… (+{len(text) - limit} chars)"


def configure_logging() -> None:
    level_name = os.environ.get("LOG_LEVEL", "DEBUG").upper()
    level = getattr(logging, level_name, logging.DEBUG)

    logging.basicConfig(
        format="%(asctime)s %(levelname)-5s %(name)s: %(message)s",
        level=logging.INFO,
        force=True,
    )
    for package in _OUR_PACKAGES:
        logging.getLogger(package).setLevel(level)
    for name, noisy_level in _NOISY.items():
        logging.getLogger(name).setLevel(noisy_level)

    logging.getLogger(__name__).info(
        "Logging configured: our packages at %s, third-party at INFO/WARNING", level_name
    )
