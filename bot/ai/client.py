import logging
import os

from bot.ai.providers import GeminiProvider, GroqProvider, build_gemini_client, build_groq_client, is_retryable
from bot.ai.tool_schema_openai import openai_tools
from bot.tools.schema import ALL_TOOLS

log = logging.getLogger(__name__)

groq_client = build_groq_client()
gemini = build_gemini_client()

_GROQ = GroqProvider(groq_client) if groq_client is not None else None
_GEMINI = GeminiProvider(gemini, ALL_TOOLS)

GROQ_MODELS = ["openai/gpt-oss-120b", "openai/gpt-oss-20b"]

# Priority order. The two Groq-hosted GPT-OSS models come first and Gemini
# behind them: the chain switches on 429/5xx, so putting a separate provider —
# separate account, separate quota, separate outage — ahead of the Gemini
# models means a Gemini rate limit no longer takes the bot down.
GEMINI_MODELS = [
    "gemma-4-31b-it",
    "gemma-4-26b-a4b-it",
    "gemini-3.6-flash",
    "gemini-3.5-flash",
    "gemini-3.5-flash-lite",
    "gemini-3.1-flash-lite",
]


def build_chain(groq_provider, gemini_provider) -> list[tuple]:
    """The GPT-OSS models first, Gemini behind them.

    A separate function rather than a module constant so the ordering can be
    asserted without a Groq key present: without one the live chain is
    Gemini-only, and a test reading the constant would quietly stop checking
    the thing it was written to check.
    """
    groq_entries = [(groq_provider, model) for model in GROQ_MODELS] if groq_provider else []
    return [(gemini_provider, model) for model in GEMINI_MODELS] + groq_entries


CHAIN = build_chain(_GROQ, _GEMINI)

# Kept for the modules that only need to know which Gemini models exist, and
# for web_search's grounded-search fallback.
MODELS = GEMINI_MODELS

# Cheap/fast model for the two-stage filter checks (active-mode relevance) so
# the primary model is never spent on a binary "is this even relevant"
# question. Deliberately not the chain's last entry: that one is the emergency
# fallback, and classify()/extract() fail closed, so a flaky classifier
# degrades silently into "never do anything" rather than erroring visibly.
CLASSIFIER_MODEL = "gemini-3.1-flash-lite"

# Groq's small model answers the same binary questions and comes from a
# different quota pool, so the classifier survives a Gemini outage too.
CLASSIFIER_CHAIN = [
    *([(_GROQ, "openai/gpt-oss-20b")] if _GROQ else []),
    (_GEMINI, CLASSIFIER_MODEL),
]


class AllModelsUnavailable(RuntimeError):
    """Every model in the chain returned 429 or 5xx.

    Distinct from an ordinary API error so the router can tell "the AI is
    unreachable right now" — which has a specific reply — from a bug.
    """


class ModelFallback:
    """Walks CHAIN, moving to the next entry on 429/5xx and staying there.

    The move is sticky because quotas are shared process-wide: having just
    learned that a model is rate-limited, trying it again on the next message
    only spends another call to be told the same thing.
    """

    def __init__(self, chain=None):
        self.chain = chain if chain is not None else CHAIN
        self.index = 0

    @property
    def current(self):
        return self.chain[self.index]

    @property
    def model(self) -> str:
        return self.current[1]

    async def start(self, prompt, *, tools=None, system_instruction=None, history=None):
        """Open a conversation on the best model that will accept it.

        Raises AllModelsUnavailable once every remaining entry has refused.
        """
        last_error = None
        while self.index < len(self.chain):
            provider, model = self.current
            try:
                chat = await provider.start(
                    model, prompt, tools=tools,
                    system_instruction=system_instruction, history=history,
                )
                return provider, model, chat
            except Exception as e:
                if not is_retryable(e):
                    raise
                last_error = e
                log.warning("%s/%s unavailable (%s), trying the next model", provider.name, model, e)
                self.index += 1
        raise AllModelsUnavailable(
            f"every model in the chain refused; last error: {last_error}"
        ) from last_error


fallback = ModelFallback()
