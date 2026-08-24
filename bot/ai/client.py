import logging
import os

from google.genai import errors

from google import genai

log = logging.getLogger(__name__)

gemini = genai.Client(api_key=os.environ["GEMINI_API_KEY"])

# Priority order: strongest first, Gemma as last resort. Sticky fallback
# on 429/5xx, never rolls back — same pattern validated in the sibling
# general-telegram-bot project's gemini_fallback.py.
MODELS = [
    "gemini-3.6-flash",
    "gemini-3.5-flash",
    "gemini-3.5-flash-lite",
    "gemini-3.1-flash-lite",
    "gemma-4-31b-it",
]

# Cheap/fast model for the two-stage filter checks (active-mode
# relevance) so the primary model is never spent on a binary "is this even
# relevant" question.
#
# Deliberately NOT MODELS[-1]: the last entry is the emergency fallback
# (Gemma), and while it does support system_instruction, response_schema
# and function calling (verified against the live API), it was measurably
# the slowest and least reliable — repeated 504 DEADLINE_EXCEEDED and
# empty completions during verification. Since classify()/extract() fail
# closed, a flaky classifier silently degrades into "never suggest
# anything" rather than erroring visibly, so this path needs the
# fast, dependable tier, not the last-resort one.
CLASSIFIER_MODEL = "gemini-3.1-flash-lite"


class ModelFallback:
    """Sends messages through a list of Gemini/Gemma models, switching to
    the next one in the list on 429 (rate limit) or 5xx (overload) and
    remembering the choice — API-key limits are shared across the whole
    process, so it never rolls back."""

    def __init__(self, client, models: list[str]):
        self.client = client
        self.models = models
        self.index = 0

    @property
    def model(self) -> str:
        return self.models[self.index]

    async def send_message(self, text, *, config=None, history=None):
        while True:
            model = self.model
            chat = self.client.aio.chats.create(model=model, config=config, history=history)
            try:
                resp = await chat.send_message(text)
                return model, chat, resp
            except errors.APIError as e:
                # e.code is None when the error body carries no numeric code
                # (APIError falls back to _get_code(response_json)); comparing
                # None >= 500 would raise TypeError from inside the handler and
                # mask the real API error, so guard before comparing.
                is_retryable = e.code == 429 or (e.code is not None and e.code >= 500)
                if is_retryable and self.index < len(self.models) - 1:
                    log.warning("%s failed (%s), switching to %s", model, e.code, self.models[self.index + 1])
                    self.index += 1
                    continue
                raise


fallback = ModelFallback(gemini, MODELS)
