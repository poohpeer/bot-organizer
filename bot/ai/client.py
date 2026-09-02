import logging
import os

from bot.ai.proxy import ProxyProvider, is_cli_backed, is_retryable, proxy

log = logging.getLogger(__name__)

# The fallback order, as ai-proxy names these models. This bot calls no
# model provider directly — there is no SDK client, no API key and no
# provider endpoint anywhere in it — so every name here is only ever a string
# handed to ai-proxy, which owns the credentials and the dialects.
#
# The two Groq-hosted GPT-OSS models come first and Gemini behind them: the
# chain advances on 429/5xx, so putting a separate provider — separate
# account, separate quota, separate outage — ahead of the Gemini models means
# a Gemini rate limit no longer takes the bot down.
#
# codex and claude sit behind the HTTP providers, not in front of them. They
# reach this bot's tools over MCP (see docs/mcp.md) rather than taking
# declarations, which is why they were out of this chain until now — an
# adapter that accepted a tools array and ignored it did not degrade, it
# fabricated.
#
# Behind, because they are the reserve. Groq and Gemini answer a tool turn in
# a few hundred tokens; measured on one task, codex spent 5 546 uncached
# input tokens against Groq's 152. The reason to keep them is that they are
# separate accounts with separate quotas, and this chain has spent whole
# evenings against Groq's 429s.
#
# codex ahead of claude because it is on a free account: tokens that cost
# nothing outrank a token count.
#
# The gemini names are the ones ai-proxy's gemini adapter actually
# accepts. Its /v1/models catalogue used to advertise three others
# (gemma-4-26b-a4b-it, gemini-3.5-flash-lite, gemini-3.1-flash-lite)
# that it then rejected with 400 model_not_found — and a 400 is not
# retryable, so reaching one aborted the whole chain rather than
# advancing past it. Half of this fallback was a landmine.
DEFAULT_PROXY_MODELS = ",".join([
    "openai/gpt-oss-120b",
    "openai/gpt-oss-20b",
    "gemini-3.7-flash",
    "gemini-3.6-flash",
    "gemini-3.5-flash",
    "gemma-4-31b-it",
    "codex:",
    "claude:sonnet",
])
PROXY_MODELS = [m.strip() for m in os.environ.get(
    "AI_PROXY_MODELS",
    DEFAULT_PROXY_MODELS,
).split(",") if m.strip()]
# What the cheap structured-JSON calls may use — the session start/stop
# decision, the topic guard, the silent capture, the title/description
# extraction.
#
# Never a CLI-backed adapter. They are driven through a command-line tool
# that answers in prose, and a request for JSON against a schema comes back
# empty: `extract() got an empty response, defaulting to {}`. Every caller of
# extract() fails closed on {}, deliberately — so an empty answer is not a
# degraded decision, it is the decision "no". Live, with codex at the front
# of the chain, that turned into a bot that answered «Что отслеживаем?» to
# «Едем в парк», «Море» and «Едем на море» in turn and could never start a
# session at all.
#
# Derived from PROXY_MODELS rather than configured separately, so reordering
# the chain — which is a thing chats and operators do — cannot take the
# classifier down with it. MCP does not help here: it carries tool calls, and
# these calls declare no tools.
CLASSIFIER_MODELS = [m for m in PROXY_MODELS if not is_cli_backed(m)]
if not CLASSIFIER_MODELS:
    log.warning(
        "AI_PROXY_MODELS holds only CLI-backed adapters (%s); the classifier will "
        "try them anyway, because no classifier at all is worse", ",".join(PROXY_MODELS),
    )
    CLASSIFIER_MODELS = list(PROXY_MODELS)

PROXY_MODEL = os.environ.get("AI_PROXY_MODEL", PROXY_MODELS[0])
CHAIN = [(proxy, model) for model in PROXY_MODELS] if proxy else []

# Kept for the modules that only need to know which Gemini models exist, and
# for web_search's grounded-search fallback.
MODELS = PROXY_MODELS

# The cheap filter checks (active-mode relevance, session start/stop) walk
# this same chain — see bot/ai/classify.py's _extract_via_chain. There is no
# separate classifier model: a CLASSIFIER_MODEL constant used to sit here and
# was only ever printed in a log line, naming a model no request went to.
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

    async def start(self, prompt, *, tools=None, system_instruction=None, history=None, mcp_url=None):
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
                    mcp_url=mcp_url,
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

# One ModelFallback per chat, because the switch is sticky: having learned a
# model is rate limited, the chain stays past it rather than spending another
# call to be told again. A single shared object made that knowledge global,
# so one busy chat pushed every other chat down the chain with it — and it
# left no way to give a chat its own order at all.
_per_chat: dict[int, ModelFallback] = {}


def fallback_for(chat_id: int, models: list[str]) -> ModelFallback:
    """The chain this chat is on. Rebuilt when its order changes, which also
    resets the stickiness — the right moment for it, since the reason to stay
    past a model no longer applies to a chain it may no longer be in."""
    wanted = list(models) if proxy else []
    existing = _per_chat.get(chat_id)
    if existing is None or [m for _p, m in existing.chain] != wanted:
        _per_chat[chat_id] = ModelFallback([(proxy, m) for m in wanted])
    return _per_chat[chat_id]


def forget_chat(chat_id: int) -> None:
    """Drop a chat's chain so the next turn rebuilds it. Called when its
    settings change."""
    _per_chat.pop(chat_id, None)
