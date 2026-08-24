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

# Cheapest model in the chain — used for the two-stage cheap-filter
# checks (R5, active-mode relevance) so the primary model is never
# spent on a binary "is this even relevant" question.
CLASSIFIER_MODEL = MODELS[-1]


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
                is_retryable = e.code == 429 or e.code >= 500
                if is_retryable and self.index < len(self.models) - 1:
                    log.warning("%s failed (%s), switching to %s", model, e.code, self.models[self.index + 1])
                    self.index += 1
                    continue
                raise


fallback = ModelFallback(gemini, MODELS)
