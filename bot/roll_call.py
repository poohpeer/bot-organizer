"""The roll call: asking everyone who hasn't answered whether they're coming.

Three rounds with a doubling gap, then a summary of who answered and who did
not. Pure functions only — no database, no Telegram — for the same reason
bot/list_render.py and bot/participant_render.py are: the wording is what
people read and argue about, and it should be checkable without a session.

Everything goes to the group chat, never to a private message. Telegram
refuses (Forbidden) to DM anyone who has never pressed Start with the bot,
which is the normal state for most group members, so a private roll call
would silently skip exactly the people it exists to reach.
"""

import os

from bot import people

# Doubling, as asked for: an hour, then two, then four. Three rounds total —
# a bot that keeps asking forever gets muted, and the point of the summary is
# to hand the chasing back to the humans once the bot has done its part.
#
# Overridable so a live check does not take seven hours; the default is what
# runs in production.
_DEFAULT_INTERVALS = "60,120,240"
ROUND_INTERVALS_MINUTES = tuple(
    int(part) for part in
    os.environ.get("ROLL_CALL_INTERVALS_MINUTES", _DEFAULT_INTERVALS).split(",")
    if part.strip()
)
MAX_ROUNDS = len(ROUND_INTERVALS_MINUTES)

_QUESTION_HEAD = "Ещё не сказали, идёте или нет:"
_QUESTION_TAIL = "Напишите, пожалуйста, да или нет."
# The blind ask. There is no way to enumerate a group's members through the
# Bot API, so anyone the bot has never seen speak cannot be named — asking the
# room at large is the only way to reach them at all.
_BLIND_ASK = "И все остальные, кого я ещё не записал, — напишите, идёте или нет."

_ANSWERED_HEAD = "Ответили:"
_UNANSWERED_HEAD = "Не ответили:"
_CLOSING_ASK = "Позаботьтесь, чтобы все ответили."
_UNKNOWN_OTHERS = (
    "Кроме них в чате ещё {count} человек, которых я не знаю — спросите их тоже."
)
_NOBODY_ANSWERED = "Никто не ответил."
_EVERYONE_ANSWERED = "Ответили все."

# The word next to each name in the summary, so a reader does not have to
# decode an icon. The icons still come from bot/participant_render.py, which
# owns them for every other list in the bot.
_ANSWER_WORDS = {
    "confirmed": "иду",
    "maybe": "может быть",
    "declined": "не иду",
}


def next_interval_minutes(rounds_done: int) -> int:
    """Minutes until the round after the one just sent.

    Clamped at the last interval rather than raising: a row that somehow ran
    past MAX_ROUNDS should still get a sane next_at, because the worker ends
    the run by looking at rounds_done, not by the clock.
    """
    index = min(max(rounds_done, 0), MAX_ROUNDS - 1)
    return ROUND_INTERVALS_MINUTES[index]


def unknown_others(chat_member_count, recorded_count: int) -> int:
    """How many people are in the chat that the roster has never heard of.

    Minus one for the bot, which getChatMemberCount counts as a member. None
    means the count has never been fetched — reporting 0 there would state as
    fact that the bot knows everyone, which is the one thing it can never
    know.
    """
    if chat_member_count is None:
        return 0
    return max(0, chat_member_count - recorded_count - 1)


_ANSWERED_STATUSES = ("confirmed", "maybe", "declined")


def split_by_answer(participants: list[dict]) -> tuple[list[dict], list[dict]]:
    """Who has answered and who hasn't.

    'maybe' counts as answered. Someone who hedged has replied, just
    non-committally; asking them again chases a response they already gave,
    which is what makes a roll call feel like nagging rather than organising.
    """
    answered = [p for p in participants if p.get("status") in _ANSWERED_STATUSES]
    unanswered = [p for p in participants if p.get("status") not in _ANSWERED_STATUSES]
    return answered, unanswered


async def send_round(telegram_bot, chat_id: int, *, answered: list[dict],
                     unanswered: list[dict], others: int, rounds_done: int) -> str:
    """Post one round, or the summary when the run is over.

    Takes the roster rather than a pool, so the tool that starts a roll call
    and the worker that continues it compose the same message from the same
    code. Two code paths would have drifted the first time either wording
    changed — and the first round is sent by one, the rest by the other.

    `rounds_done` is how many rounds went out *before* this one. Returns
    "asked" or "summarised"; a failed send raises so the caller can put its
    claim back.
    """
    # Either nobody is left to chase or every round is spent. Both end the run
    # the same way, and ending early matters: a group that all answered after
    # the first round should get the summary then, not two more questions
    # naming nobody.
    if not unanswered or rounds_done >= MAX_ROUNDS:
        await telegram_bot.send_message(
            chat_id=chat_id, text=render_summary(answered, unanswered, others)
        )
        return "summarised"
    await telegram_bot.send_message(
        chat_id=chat_id, text=render_question(unanswered, others)
    )
    return "asked"


def _name(person: dict) -> str:
    return people.mention(person.get("display_name"), person.get("username"))


def render_question(unanswered: list[dict], others: int = 0) -> str:
    """One round's question, naming everyone still silent.

    Names, not "все, кто не ответил": an @handle is a live Telegram mention
    and actually notifies the person, which is the entire mechanism by which
    this differs from posting into the void.
    """
    lines = [_QUESTION_HEAD]
    lines.extend(_name(p) for p in unanswered)
    lines.append("")
    lines.append(_QUESTION_TAIL)
    if others > 0:
        lines.append(_BLIND_ASK)
    return "\n".join(lines)


def render_summary(answered: list[dict], unanswered: list[dict], others: int = 0) -> str:
    """The message after the last round: who answered, who didn't, and a
    handover.

    Both headings always appear, with an explicit line when a side is empty.
    A missing heading reads as the bot having forgotten to check, which is
    exactly the doubt the summary exists to remove.
    """
    blocks = []

    answered_lines = [_ANSWERED_HEAD]
    if answered:
        answered_lines.extend(
            f"{_name(p)} — {_ANSWER_WORDS.get(p.get('status'), 'ответил')}"
            for p in answered
        )
    else:
        answered_lines.append(_NOBODY_ANSWERED)
    blocks.append("\n".join(answered_lines))

    unanswered_lines = [_UNANSWERED_HEAD]
    if unanswered:
        unanswered_lines.extend(_name(p) for p in unanswered)
    else:
        unanswered_lines.append(_EVERYONE_ANSWERED)
    blocks.append("\n".join(unanswered_lines))

    tail = [_CLOSING_ASK]
    if others > 0:
        tail.append(_UNKNOWN_OTHERS.format(count=others))
    blocks.append("\n".join(tail))

    return "\n\n".join(blocks)
