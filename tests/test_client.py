from unittest.mock import AsyncMock, MagicMock

import pytest
from google.genai import errors

from bot.ai.client import ModelFallback


def _client_raising(exc, then=None):
    """A genai-like client whose chat.send_message raises `exc` for the first
    model and (optionally) succeeds for the next."""
    client = MagicMock()
    chats = []

    def create(model, config=None, history=None):
        chat = MagicMock()
        if len(chats) == 0:
            chat.send_message = AsyncMock(side_effect=exc)
        else:
            chat.send_message = AsyncMock(return_value=then)
        chats.append(chat)
        return chat

    client.aio.chats.create = create
    return client


async def test_fallback_advances_on_429():
    ok = MagicMock(text="hi")
    err = errors.APIError(code=429, response_json={"error": {"code": 429}}, response=None)
    fb = ModelFallback(_client_raising(err, then=ok), ["a", "b"])

    model, _chat, resp = await fb.send_message("x")

    assert (model, resp) == ("b", ok)
    assert fb.index == 1


async def test_fallback_does_not_crash_when_error_has_no_code():
    """APIError.code is None when the body carries no numeric code; comparing
    None >= 500 would raise TypeError from inside the except block and mask
    the real error."""
    err = errors.APIError(code=None, response_json={"error": {"message": "boom"}}, response=None)
    fb = ModelFallback(_client_raising(err), ["a", "b"])

    with pytest.raises(errors.APIError):
        await fb.send_message("x")

    assert fb.index == 0  # non-retryable: stayed put rather than burning a model


async def test_fallback_reraises_non_retryable_400():
    err = errors.APIError(code=400, response_json={"error": {"code": 400}}, response=None)
    fb = ModelFallback(_client_raising(err), ["a", "b"])

    with pytest.raises(errors.APIError):
        await fb.send_message("x")

    assert fb.index == 0
