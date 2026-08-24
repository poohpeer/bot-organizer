"""The single gate that decides whether the bot may act.

Deliberately model-free. Whether the bot speaks is answered by comparing ids
and entity offsets, so it is exhaustively testable and cannot drift with a
prompt or a model upgrade. The model is consulted only *after* this gate says
yes, and only to work out what was asked.
"""

from telegram import MessageEntity
from telegram.constants import ChatMemberStatus

# Statuses that mean the bot is actually in the chat. RESTRICTED is excluded on
# purpose: it carries a separate is_member flag, and a restricted bot is not in
# a position to organize anything.
_PRESENT = frozenset({
    ChatMemberStatus.MEMBER,
    ChatMemberStatus.ADMINISTRATOR,
    ChatMemberStatus.OWNER,
})


def addressed_to_bot(message, bot_id: int, bot_username: str) -> bool:
    """True when this message is directed at the bot: an @mention of its exact
    username, or a reply to something the bot itself said."""
    reply = message.reply_to_message
    if reply is not None and reply.from_user is not None and reply.from_user.id == bot_id:
        return True

    handle = f"@{bot_username}".lower()
    # Text and caption entities need different parsers — parse_entity raises
    # on a message that only has a caption, so a photo captioned "@bot вот тут"
    # would blow up instead of being answered.
    sources = (
        (message.entities or (), message.parse_entity),
        (message.caption_entities or (), message.parse_caption_entity),
    )
    for entities, parse in sources:
        for entity in entities:
            if entity.type == MessageEntity.MENTION:
                # parse_* rather than slicing the text: Telegram counts entity
                # offsets in UTF-16 code units, so a single emoji anywhere
                # earlier in the message shifts a naive slice by one and the
                # comparison silently stops matching.
                if parse(entity).lower() == handle:
                    return True
            elif entity.type == MessageEntity.TEXT_MENTION:
                # A mention of a user without a public username, carrying the
                # user object directly — the form that still resolves if the
                # bot is renamed.
                if entity.user is not None and entity.user.id == bot_id:
                    return True
    return False


def bot_was_added(chat_member_updated, bot_id: int) -> bool:
    """True when this my_chat_member update is the bot itself joining a chat.

    Compares old and new status rather than just reading the new one: a
    promotion to administrator also arrives as a my_chat_member update, and
    greeting the chat again on every permission change would be noise.
    """
    new = chat_member_updated.new_chat_member
    if new.user.id != bot_id:
        return False
    old = chat_member_updated.old_chat_member
    return new.status in _PRESENT and old.status not in _PRESENT
