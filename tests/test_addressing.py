import datetime as dt

import pytest
from telegram import Chat, ChatMemberAdministrator, ChatMemberLeft, ChatMemberMember, ChatMemberUpdated, Message, MessageEntity, User

from bot.addressing import addressed_to_bot, bot_was_added

BOT_ID = 4242
BOT_USERNAME = "orgbot"

_BOT = User(id=BOT_ID, first_name="Organizer", is_bot=True, username=BOT_USERNAME)
_HUMAN = User(id=7, first_name="Sasha", is_bot=False)


def _utf16_offset(text: str, marker: str) -> int:
    """Telegram reports entity offsets in UTF-16 code units, which is what the
    tests must feed in for them to mean anything."""
    return len(text[: text.index(marker)].encode("utf-16-le")) // 2


def _message(text=None, entities=(), reply_to=None, caption=None, caption_entities=()):
    return Message(
        message_id=1,
        date=dt.datetime.now(dt.timezone.utc),
        chat=Chat(id=-100, type="group"),
        from_user=_HUMAN,
        text=text,
        entities=list(entities),
        caption=caption,
        caption_entities=list(caption_entities),
        reply_to_message=reply_to,
    )


def _mention(text, handle="@orgbot", entity_type=MessageEntity.MENTION, user=None):
    offset = _utf16_offset(text, handle)
    entity = MessageEntity(type=entity_type, offset=offset, length=len(handle), user=user)
    return _message(text=text, entities=[entity])


def test_plain_mention_is_addressed():
    assert addressed_to_bot(_mention("@orgbot помоги с пикником"), BOT_ID, BOT_USERNAME) is True


def test_mention_in_the_middle_is_addressed():
    assert addressed_to_bot(_mention("слушай @orgbot, что там по списку"), BOT_ID, BOT_USERNAME) is True


def test_ordinary_chatter_is_not_addressed():
    assert addressed_to_bot(_message(text="надо купить помидоры"), BOT_ID, BOT_USERNAME) is False


def test_message_without_text_is_not_addressed():
    assert addressed_to_bot(_message(), BOT_ID, BOT_USERNAME) is False


def test_reply_to_the_bot_is_addressed():
    bot_message = Message(
        message_id=99, date=dt.datetime.now(dt.timezone.utc),
        chat=Chat(id=-100, type="group"), from_user=_BOT, text="Что берём с собой?",
    )
    assert addressed_to_bot(_message(text="я возьму мангал", reply_to=bot_message), BOT_ID, BOT_USERNAME) is True


def test_reply_to_another_person_is_not_addressed():
    someone_else = Message(
        message_id=99, date=dt.datetime.now(dt.timezone.utc),
        chat=Chat(id=-100, type="group"), from_user=_HUMAN, text="я возьму мангал",
    )
    assert addressed_to_bot(_message(text="отлично", reply_to=someone_else), BOT_ID, BOT_USERNAME) is False


def test_a_similarly_named_bot_is_not_us():
    """@orgbot is a prefix of @orgbot_test — a substring check would answer
    yes here and the bot would butt into another bot's conversation."""
    text = "@orgbot_test добавь помидоры"
    entity = MessageEntity(type=MessageEntity.MENTION, offset=0, length=len("@orgbot_test"))
    assert addressed_to_bot(_message(text=text, entities=[entity]), BOT_ID, BOT_USERNAME) is False


def test_mention_after_an_emoji_is_addressed():
    """An emoji is one Python character but two UTF-16 code units, so slicing
    the text by Telegram's offset lands one character off and the comparison
    silently fails. This is the case that makes parse_entity mandatory."""
    assert addressed_to_bot(_mention("🎉 @orgbot погнали"), BOT_ID, BOT_USERNAME) is True


def test_mention_after_several_emoji_is_addressed():
    assert addressed_to_bot(_mention("🎉🎂🥳 @orgbot когда собираемся"), BOT_ID, BOT_USERNAME) is True


def test_username_match_is_case_insensitive():
    assert addressed_to_bot(_mention("@OrgBot привет", handle="@OrgBot"), BOT_ID, BOT_USERNAME) is True


def test_text_mention_of_the_bot_is_addressed():
    """A mention rendered as a link to a user carries the user object rather
    than a username — the form that still resolves if the bot is renamed."""
    msg = _mention("Organizer помоги", handle="Organizer",
                   entity_type=MessageEntity.TEXT_MENTION, user=_BOT)
    assert addressed_to_bot(msg, BOT_ID, BOT_USERNAME) is True


def test_text_mention_of_someone_else_is_not_addressed():
    msg = _mention("Sasha помоги", handle="Sasha",
                   entity_type=MessageEntity.TEXT_MENTION, user=_HUMAN)
    assert addressed_to_bot(msg, BOT_ID, BOT_USERNAME) is False


def test_mention_in_a_photo_caption_is_addressed():
    caption = "@orgbot вот это место"
    entity = MessageEntity(type=MessageEntity.MENTION, offset=0, length=len("@orgbot"))
    assert addressed_to_bot(
        _message(caption=caption, caption_entities=[entity]), BOT_ID, BOT_USERNAME
    ) is True


def _member_update(old_status, new_status, user=_BOT):
    classes = {
        "left": ChatMemberLeft, "member": ChatMemberMember,
        "administrator": ChatMemberAdministrator,
    }
    def make(status):
        if status == "administrator":
            return ChatMemberAdministrator(
                user=user, can_be_edited=False, is_anonymous=False,
                can_manage_chat=False, can_delete_messages=False,
                can_manage_video_chats=False, can_restrict_members=False,
                can_promote_members=False, can_change_info=False,
                can_invite_users=False, can_post_stories=False,
                can_edit_stories=False, can_delete_stories=False,
            )
        return classes[status](user=user)

    return ChatMemberUpdated(
        chat=Chat(id=-100, type="group"), from_user=_HUMAN,
        date=dt.datetime.now(dt.timezone.utc),
        old_chat_member=make(old_status), new_chat_member=make(new_status),
    )


def test_bot_joining_the_chat_is_detected():
    assert bot_was_added(_member_update("left", "member"), BOT_ID) is True


def test_bot_being_promoted_is_not_a_join():
    """A promotion arrives as the same update type. Greeting the chat again on
    every permission change would be noise."""
    assert bot_was_added(_member_update("member", "administrator"), BOT_ID) is False


def test_bot_leaving_is_not_a_join():
    assert bot_was_added(_member_update("member", "left"), BOT_ID) is False


def test_another_user_joining_is_not_the_bot():
    assert bot_was_added(_member_update("left", "member", user=_HUMAN), BOT_ID) is False
