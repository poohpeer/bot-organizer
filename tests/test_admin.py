"""/admin: who may use it, and what the buttons do."""

from unittest.mock import AsyncMock

import bot.admin as admin
import bot.ai.client as ai_client
import bot.settings as settings


class _Msg:
    def __init__(self, chat_id=-100, user_id=7):
        self.chat = type("C", (), {"id": chat_id})()
        self.from_user = type("U", (), {"id": user_id})() if user_id else None


class _Query:
    def __init__(self, data, chat_id=-100, user_id=7):
        self.data = data
        self.message = _Msg(chat_id, user_id)
        self.from_user = self.message.from_user
        self.answer = AsyncMock()
        self.edit_message_text = AsyncMock()


def _bot(status="administrator"):
    bot = AsyncMock()
    bot.get_chat_member.return_value = type("M", (), {"status": status})()
    return bot


# --- who may use it -------------------------------------------------------

async def test_a_member_is_told_no_and_shown_nothing(db_pool):
    """The chain decides how every message in the group is answered. Leaving
    it open would let one person change the bot for everyone else."""
    bot = _bot(status="member")

    await admin.handle_command(db_pool, bot, _Msg())

    assert "администраторам" in bot.send_message.await_args.kwargs["text"]
    assert "reply_markup" not in bot.send_message.await_args.kwargs


async def test_an_administrator_gets_the_menu(db_pool):
    bot = _bot()

    await admin.handle_command(db_pool, bot, _Msg())

    assert bot.send_message.await_args.kwargs["reply_markup"] is not None


async def test_a_button_press_is_checked_again_not_just_the_open(db_pool):
    """The keyboard stays live in the chat, so anyone can press it, and
    whoever opened it may since have been demoted."""
    bot = _bot(status="member")
    query = _Query(admin._CHAIN)

    await admin.handle_callback(db_pool, bot, query)

    query.answer.assert_awaited_once()
    assert query.answer.await_args.kwargs.get("show_alert") is True
    query.edit_message_text.assert_not_awaited()


async def test_a_failed_admin_check_answers_no(db_pool):
    """A settings menu is the wrong place to fail open."""
    bot = AsyncMock()
    bot.get_chat_member.side_effect = RuntimeError("telegram is unwell")

    assert await admin.is_chat_admin(bot, -100, 7) is False


# --- the buttons ----------------------------------------------------------

def test_moving_an_entry_up_swaps_it_with_the_one_above():
    chain = list(ai_client.PROXY_MODELS)

    moved = admin.apply_action(chain, "up", 1)

    assert moved[:2] == [chain[1], chain[0]]


def test_moving_the_top_entry_up_changes_nothing():
    """A no-op rather than an error: the button is still there to press."""
    chain = list(ai_client.PROXY_MODELS)

    assert admin.apply_action(chain, "up", 0) == chain


def test_moving_the_last_entry_down_changes_nothing():
    chain = list(ai_client.PROXY_MODELS)

    assert admin.apply_action(chain, "down", len(chain) - 1) == chain


def test_toggling_removes_and_restores_a_model():
    chain = list(ai_client.PROXY_MODELS)

    without = admin.apply_action(chain, "tog", 0)
    assert chain[0] not in without

    # The disabled one shows below the enabled ones, so its position moved.
    shown = without + [m for m in ai_client.PROXY_MODELS if m not in without]
    restored = admin.apply_action(without, "tog", shown.index(chain[0]))
    assert chain[0] in restored


def test_a_disabled_model_is_still_listed_so_it_can_be_turned_back_on():
    """A menu that hides what you disabled cannot undo itself."""
    chain = list(ai_client.PROXY_MODELS)[1:]

    body, keyboard = admin.chain_view(chain)

    assert ai_client.PROXY_MODELS[0] in body
    assert "выключена" in body
    assert len(keyboard.inline_keyboard) == len(ai_client.PROXY_MODELS) + 1


def test_turning_everything_off_says_so():
    body, _ = admin.chain_view([])

    assert "не сможет ответить" in body


def test_an_out_of_range_position_is_ignored():
    """Callback data comes from a keyboard that may be older than the list."""
    chain = list(ai_client.PROXY_MODELS)

    assert admin.apply_action(chain, "up", 99) == chain
    assert admin.apply_action(chain, "tog", -1) == chain
