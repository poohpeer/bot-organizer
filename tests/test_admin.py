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

def test_the_first_press_makes_a_model_first():
    """The whole point of the rewrite. Under the ▲▼ pair it replaced,
    lifting the last model to the front took seven presses."""
    last = ai_client.PROXY_MODELS[-1]

    chosen = admin.pick([], len(ai_client.PROXY_MODELS) - 1)

    assert chosen == [last]


def test_each_press_appends_in_the_order_pressed():
    catalogue = list(ai_client.PROXY_MODELS)
    chosen = []

    # Pressed back to front. The shown order puts chosen models first, so
    # after each press the remaining ones sit below them.
    for model in reversed(catalogue[:3]):
        shown = chosen + [m for m in catalogue if m not in chosen]
        chosen = admin.pick(chosen, shown.index(model))

    assert chosen == [catalogue[2], catalogue[1], catalogue[0]]


def test_pressing_a_chosen_model_takes_it_out_and_renumbers_the_rest():
    catalogue = list(ai_client.PROXY_MODELS)
    chosen = catalogue[:3]

    without = admin.pick(chosen, 1)

    assert without == [catalogue[0], catalogue[2]], "the third moves up to second"


def test_taking_one_out_and_pressing_it_again_puts_it_last():
    """Two presses to move a model to the end of the chain."""
    catalogue = list(ai_client.PROXY_MODELS)
    chosen = catalogue[:3]

    without = admin.pick(chosen, 0)
    shown = without + [m for m in catalogue if m not in without]
    again = admin.pick(without, shown.index(catalogue[0]))

    assert again == [catalogue[1], catalogue[2], catalogue[0]]


def test_an_unchosen_model_is_still_listed_so_it_can_be_picked():
    """A menu that hides what you did not pick cannot undo itself."""
    chain = list(ai_client.PROXY_MODELS)[1:]

    body, keyboard = admin.chain_view(chain)

    assert ai_client.PROXY_MODELS[0] in body
    assert len(keyboard.inline_keyboard) == len(ai_client.PROXY_MODELS) + 1
    assert all(len(row) == 1 for row in keyboard.inline_keyboard[:-1]), \
        "one button per model: the names are too long to share a row"


def test_the_chain_is_numbered_in_the_order_it_runs():
    catalogue = list(ai_client.PROXY_MODELS)

    body, _ = admin.chain_view([catalogue[2], catalogue[0]])

    assert body.index("1\ufe0f\u20e3") < body.index("2\ufe0f\u20e3")
    assert body.index(catalogue[2]) < body.index(catalogue[0])


def test_choosing_nothing_says_which_default_is_standing_in():
    """Not an error state: bot/settings.py falls back to the deployment
    default, so the menu says so instead of warning about a broken bot."""
    body, _ = admin.chain_view([])

    assert "по умолчанию" in body
    for model in ai_client.PROXY_MODELS:
        assert model in body


def test_an_out_of_range_position_is_ignored():
    """Callback data comes from a keyboard that may be older than the list."""
    chain = list(ai_client.PROXY_MODELS)

    assert admin.pick(chain, 99) == chain
    assert admin.pick(chain, -1) == chain


# --- pressing them for real -----------------------------------------------

async def test_pressing_a_model_stores_it_as_the_whole_chain(db_pool):
    """A chat on the default that picks one model gets that one model — the
    screen says "нажимайте в том порядке, в каком бот должен их звать", and
    quietly keeping the other seven behind it would not be that."""
    bot = _bot()
    last = ai_client.PROXY_MODELS[-1]

    await admin.handle_callback(
        db_pool, bot, _Query(f"{admin._PICK}{len(ai_client.PROXY_MODELS) - 1}")
    )

    assert await settings.get_stored_chain(db_pool, -100) == [last]


async def test_pressing_it_again_takes_it_back_out(db_pool):
    bot = _bot()
    first = ai_client.PROXY_MODELS[0]
    await settings.set_provider_chain(db_pool, -100, [first])

    await admin.handle_callback(db_pool, bot, _Query(f"{admin._PICK}0"))

    assert await settings.get_stored_chain(db_pool, -100) == []
    # And the bot is still able to answer.
    assert await settings.get_provider_chain(db_pool, -100) == list(ai_client.PROXY_MODELS)


def test_the_reset_button_says_what_it_does():
    """It said "Очистить", and only one of that word's two readings is ever
    what anyone wants. Pressed in the sense of "done", it threw away the
    order just built — which looks exactly like the setting failing to
    survive a restart, and was reported as such."""
    _, keyboard = admin.chain_view(list(ai_client.PROXY_MODELS)[:2])

    labels = [button.text for row in keyboard.inline_keyboard for button in row]
    assert "По умолчанию" in labels
    assert "Очистить" not in labels


async def test_clearing_deletes_the_row_rather_than_storing_a_copy(db_pool):
    bot = _bot()
    await settings.set_provider_chain(db_pool, -100, [ai_client.PROXY_MODELS[0]])

    await admin.handle_callback(db_pool, bot, _Query(admin._RESET))

    assert await settings.get_stored_chain(db_pool, -100) is None


# --- closing it -----------------------------------------------------------

async def test_closing_the_menu_leaves_nothing_behind(db_pool):
    """It used to write "Закрыто." over itself — a message about the bot's own
    furniture, answering a question nobody asked and staying in the chat
    forever, next to the plans people came to read."""
    bot = _bot()
    query = _Query(admin._CLOSE)
    query.delete_message = AsyncMock()

    await admin.handle_callback(db_pool, bot, query)

    query.delete_message.assert_awaited_once()
    query.edit_message_text.assert_not_awaited()
    bot.send_message.assert_not_awaited()


async def test_a_menu_too_old_to_delete_at_least_loses_its_buttons(db_pool):
    """A bot may only delete its own message for 48 hours. A menu that
    outlives that must still stop being pressable — and still without writing
    anything new."""
    bot = _bot()
    query = _Query(admin._CLOSE)
    query.delete_message = AsyncMock(side_effect=RuntimeError("message can't be deleted"))
    query.edit_message_reply_markup = AsyncMock()

    await admin.handle_callback(db_pool, bot, query)

    assert query.edit_message_reply_markup.await_args.kwargs["reply_markup"] is None
    query.edit_message_text.assert_not_awaited()


async def test_a_menu_that_can_be_neither_deleted_nor_edited_does_not_raise(db_pool):
    """Whatever went wrong, it is the bot's own menu — not worth turning into
    an error the group sees."""
    bot = _bot()
    query = _Query(admin._CLOSE)
    query.delete_message = AsyncMock(side_effect=RuntimeError("nope"))
    query.edit_message_reply_markup = AsyncMock(side_effect=RuntimeError("also nope"))

    await admin.handle_callback(db_pool, bot, query)

    bot.send_message.assert_not_awaited()


# --- the bot's owner ------------------------------------------------------

async def test_the_owner_is_an_administrator_everywhere(db_pool, monkeypatch):
    """The models this bot calls are spent from the owner's account, so the
    provider chain is a decision about their money — they configure it in
    every chat the bot was added to, whether or not they run that chat."""
    monkeypatch.setenv("BOT_OWNER_ID", "91237884")
    bot = _bot(status="member")

    assert await admin.is_chat_admin(bot, -100, 91237884) is True


async def test_the_owner_is_not_asked_about(db_pool, monkeypatch):
    """Checked before Telegram, so the answer cannot be lost to a call that
    fails — the one thing this rule must never depend on."""
    monkeypatch.setenv("BOT_OWNER_ID", "91237884")
    bot = AsyncMock()
    bot.get_chat_member.side_effect = RuntimeError("telegram is unwell")

    assert await admin.is_chat_admin(bot, -100, 91237884) is True
    bot.get_chat_member.assert_not_awaited()


async def test_everyone_else_is_still_asked_about(db_pool, monkeypatch):
    monkeypatch.setenv("BOT_OWNER_ID", "91237884")
    bot = _bot(status="member")

    assert await admin.is_chat_admin(bot, -100, 7) is False
    bot.get_chat_member.assert_awaited_once()


async def test_no_owner_configured_means_no_owner(db_pool, monkeypatch):
    """A deployment nobody owns personally: only a chat's own administrators
    qualify. Defaulting to somebody would hand a stranger every chat."""
    monkeypatch.delenv("BOT_OWNER_ID", raising=False)
    bot = _bot(status="member")

    assert await admin.is_chat_admin(bot, -100, 91237884) is False


async def test_a_malformed_owner_id_is_no_owner(db_pool, monkeypatch):
    """Rather than crashing every permission check on a typo in a ConfigMap."""
    monkeypatch.setenv("BOT_OWNER_ID", "не число")
    bot = _bot(status="member")

    assert await admin.is_chat_admin(bot, -100, 91237884) is False
