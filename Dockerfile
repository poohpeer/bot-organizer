FROM ghcr.io/astral-sh/uv:python3.12-bookworm-slim

WORKDIR /app

# Dependencies before source: the layer cache survives every code change, so a
# rebuild after editing a handler doesn't re-resolve the lockfile.
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev

COPY bot ./bot
COPY db ./db
COPY worker ./worker

# The worker service overrides this with worker.main — one image, one set of
# dependencies, two entrypoints.
CMD ["uv", "run", "--no-sync", "python", "-m", "bot.main"]
