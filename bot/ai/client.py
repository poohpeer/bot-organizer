import logging
import os

from bot.ai.proxy import ProxyError, ProxyProvider, proxy
from bot.ai.providers import is_retryable

log = logging.getLogger(__name__)

groq_client = None
gemini = None

# One entry, and no model name after the prefix. A ChatGPT-authenticated
# Codex CLI only runs the account's own default model — any explicit name is
# rejected with "The '<name>' model is not supported when using Codex with a
# ChatGPT account" — and ai-proxy's codex adapter never passes -m regardless.
# The three names here previously all resolved to that same single model, so
# the chain retried the identical request three times believing each was a
# different fallback, burning two extra ~4s CLI invocations per outage.
CODEX_MODELS = ["codex:"]
CLAUDE_MODELS = ["claude:opus", "claude:sonnet", "claude:haiku"]
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


DEFAULT_PROXY_MODELS = ",".join(
    CODEX_MODELS + CLAUDE_MODELS + GROQ_MODELS + [
        "gemma-4-31b-it", "gemma-4-26b-a4b-it", "gemini-3.6-flash",
        "gemini-3.5-flash", "gemini-3.5-flash-lite", "gemini-3.1-flash-lite",
    ]
)
PROXY_MODELS = [m.strip() for m in os.environ.get(
    "AI_PROXY_MODELS",
    DEFAULT_PROXY_MODELS,
).split(",") if m.strip()]
PROXY_MODEL = os.environ.get("AI_PROXY_MODEL", PROXY_MODELS[0])
CHAIN = [(proxy, model) for model in PROXY_MODELS] if proxy else []

# Kept for the modules that only need to know which Gemini models exist, and
# for web_search's grounded-search fallback.
MODELS = PROXY_MODELS

# Cheap/fast model for the two-stage filter checks (active-mode relevance) so
# the primary model is never spent on a binary "is this even relevant"
# question. Deliberately not the chain's last entry: that one is the emergency
# fallback, and classify()/extract() fail closed, so a flaky classifier
# degrades silently into "never do anything" rather than erroring visibly.
CLASSIFIER_MODEL = "gemini-3.1-flash-lite"

# Groq's small model answers the same binary questions and comes from a
# different quota pool, so the classifier survives a Gemini outage too.
CLASSIFIER_CHAIN = [(proxy, PROXY_MODEL)] if proxy else []


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
                if not ((isinstance(e, ProxyError) and (e.status == 429 or e.status >= 500))
                        or is_retryable(e)):
                    raise
                last_error = e
                log.warning("%s/%s unavailable (%s), trying the next model", provider.name, model, e)
                self.index += 1
        raise AllModelsUnavailable(
            f"every model in the chain refused; last error: {last_error}"
        ) from last_error


fallback = ModelFallback()
