"""What a turn actually did, and what that obliges the bot to say.

Two things a chat model gets wrong often enough to need enforcing in code:
it answers around a block a renderer already produced, and it goes quiet
after changing something. The first has a guard in bot/ai/tool_loop.py; both
now live here, because the CLI providers run their tools over MCP
(bot/mcp_server.py) and so bypass the tool loop entirely — the guards were
only ever on one of the two paths.

Live, that gap cost a whole exchange. The bot said "Пиво уже есть: 1 бут.
Прибавить не могу — скажи, сколько бутылок должно быть всего.", the person
answered "Две бутылки пива", codex called list_add — the update landed in the
database — and then answered "<silent>". Nothing was sent to the chat. From
the group's side the bot had simply stopped responding.
"""

import logging
from dataclasses import dataclass, field

log = logging.getLogger(__name__)

# Fields whose value is already the finished, human-facing block of text —
# produced by a deterministic renderer (bot/list_render.py,
# bot/participant_render.py, bot/status_render.py), not by the model.
_VERBATIM_FIELDS = ("report", "rendered")

# Tools that only read. Calling one changes nothing, so a turn made only of
# these may end in silence.
READ_ONLY_TOOLS = frozenset({
    "get_facts", "list_show", "get_participants", "reminder_list",
    "event_status", "get_chat_info", "archive_lookup", "web_search",
    "maps_lookup", "weather_lookup",
})

# Tools that deliver a message themselves. Silence in the group afterwards is
# the point — send_private_message answered in a DM precisely because the
# person asked not to be answered in the group — so these must not count as
# "changed something and said nothing".
TOOLS_THAT_SPEAK_FOR_THEMSELVES = frozenset({
    "send_private_message", "broadcast_message", "send_location",
    "nudge_unconfirmed_participants",
})

# What to say when a turn changed something, produced no rendered block, and
# the model chose silence. Short on purpose: the alternative is nothing at
# all, not a better sentence.
ACKNOWLEDGEMENT = "Записал."


def verbatim_block(result: dict) -> str | None:
    """The finished block a result carries, if relaying it is the answer.

    A result that also carries ask_user is asking the model to say something
    the block cannot say. Live: told an amount was already set, the model
    answered "На списке уже есть 1 кг бананов; добавить нельзя. Скажите,
    какое общее количество нужно" — correct, and the guard below threw it
    away and sent the bare list instead, because the list was in the result.
    Refusals come with an explanation or they are not refusals.
    """
    if not isinstance(result, dict) or result.get("ask_user"):
        return None
    for name in _VERBATIM_FIELDS:
        value = result.get(name)
        if isinstance(value, str) and value.strip():
            return value
    return None


def honour_verbatim(reply_text: str, block: str | None) -> str:
    """Make "relay the rendered block character-for-character" true in code.

    The instruction says it, and the model does not reliably obey: asked
    "покажи список" it called list_show, received the rendered list, called
    event_status, received the full report — and answered "Больше нет
    элементов в списке. Что ещё нужно?", discarding both while the data sat
    in front of it. That is the same failure the renderers exist to prevent,
    one layer up.

    Substitution only when the block is *entirely* absent from the reply. A
    model that included it and added something — the roster-gap remark, a
    short lead-in — has complied, and overwriting that would throw away real
    information to satisfy a rule about formatting.
    """
    if not block or block in reply_text:
        return reply_text
    log.warning("Model dropped a rendered block from its reply; sending the block instead")
    return block


@dataclass
class TurnRecord:
    """Which tools ran during one turn, and what they left for the reply.

    Kept separate from the grant that identifies the session: a grant says
    who may act, this says what was done. Mutable because it is filled in as
    the turn runs and read once, after it ends.
    """

    tools_called: list[str] = field(default_factory=list)
    verbatim: str | None = None
    # A sentence the tool wrote for the case where the model says nothing.
    # Distinct from verbatim, which is relayed over a model that talked
    # around it, and from ask_user, which is instructions to the model and
    # is in English. Live, "добавь бутылку пива" replaced 2 бут. with 1 бут.
    # and the model answered "<silent>": "Записал." would have been true and
    # useless, while "пиво: было 2 бут., стало 1 бут." is the one thing the
    # group needed to see to catch it.
    say: str | None = None

    def record(self, name: str, result) -> None:
        self.tools_called.append(name)
        # Latest wins: it reflects the freshest state, and it is the last
        # thing the model itself chose to go and fetch.
        block = verbatim_block(result)
        if block:
            self.verbatim = block
        if isinstance(result, dict):
            said = result.get("say")
            if isinstance(said, str) and said.strip():
                self.say = said

    def changed_something(self) -> bool:
        return any(
            name not in READ_ONLY_TOOLS and name not in TOOLS_THAT_SPEAK_FOR_THEMSELVES
            for name in self.tools_called
        )
