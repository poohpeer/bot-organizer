"""/admin: who may use it, and what the buttons do."""

from unittest.mock import AsyncMock

import pytest

import bot.admin as admin
import bot.ai.client as ai_client
import bot.settings as settings


# Whoever the tests mean by "the owner". Set for every test by the autouse
# fixture below, because the gate is now ownership and nothing else — without
# it every button test would be testing the refusal path.
OWNER_ID = 91237884


@pytest.fixture(autouse=True)
def _owner(monkeypatch):
    monkeypatch.setenv("BOT_OWNER_ID", str(OWNER_ID))


class _Msg:
    def __init__(self, chat_id=-100, user_id=OWNER_ID):
        self.chat = type("C", (), {"id": chat_id})()
        self.from_user = type("U", (), {"id": user_id})() if user_id else None


class _Query:
    def __init__(self, data, chat_id=-100, user_id=OWNER_ID):
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
    """The chain is spent from the owner's account, so nobody else may
    reorder it — however senior they are in the group."""
    bot = _bot(status="member")

    await admin.handle_command(db_pool, bot, _Msg(user_id=7))

    assert "владельцу" in bot.send_message.await_args.kwargs["text"]
    assert "reply_markup" not in bot.send_message.await_args.kwargs


async def test_a_group_administrator_is_told_no_too(db_pool):
    """The rule that changed: running the group is not running the bot."""
    bot = _bot(status="administrator")

    await admin.handle_command(db_pool, bot, _Msg(user_id=7))

    assert "владельцу" in bot.send_message.await_args.kwargs["text"]
    assert "reply_markup" not in bot.send_message.await_args.kwargs


async def test_the_owner_gets_the_menu(db_pool):
    bot = _bot()

    await admin.handle_command(db_pool, bot, _Msg())

    assert bot.send_message.await_args.kwargs["reply_markup"] is not None


async def test_a_button_press_is_checked_again_not_just_the_open(db_pool):
    """The keyboard stays live in the chat, so anyone in the group can press
    it long after the owner opened it."""
    bot = _bot(status="administrator")
    query = _Query(admin._CHAIN, user_id=7)

    await admin.handle_callback(db_pool, bot, query)

    query.answer.assert_awaited_once()
    assert query.answer.await_args.kwargs.get("show_alert") is True
    query.edit_message_text.assert_not_awaited()


async def test_telegram_is_never_asked_at_all(db_pool):
    """Group status stopped mattering, so there is nothing left to ask about
    — and the check can no longer be lost to a call that fails."""
    bot = AsyncMock()
    bot.get_chat_member.side_effect = RuntimeError("telegram is unwell")

    await admin.handle_command(db_pool, bot, _Msg())

    bot.get_chat_member.assert_not_awaited()
    assert bot.send_message.await_args.kwargs["reply_markup"] is not None


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

def test_the_owner_qualifies_everywhere():
    """The models this bot calls are spent from the owner's account, so the
    provider chain is a decision about their money — they configure it in
    every chat the bot was added to, whether or not they run that chat."""
    assert admin.is_bot_owner(OWNER_ID) is True


def test_a_group_administrator_does_not_qualify():
    """Group status is no longer consulted at all, in any chat."""
    assert admin.is_bot_owner(7) is False


def test_an_anonymous_sender_does_not_qualify():
    assert admin.is_bot_owner(None) is False


def test_no_owner_configured_means_nobody_qualifies(monkeypatch):
    """A deployment nobody owns personally: the menu is unavailable, rather
    than falling back to whoever happens to run the group."""
    monkeypatch.delenv("BOT_OWNER_ID", raising=False)

    assert admin.is_bot_owner(OWNER_ID) is False


def test_a_malformed_owner_id_is_no_owner(monkeypatch):
    """Rather than crashing every permission check on a typo in a ConfigMap."""
    monkeypatch.setenv("BOT_OWNER_ID", "не число")

    assert admin.is_bot_owner(OWNER_ID) is False


# --- the topic guard ------------------------------------------------------

def _button_labels(markup) -> list[str]:
    return [b.text for row in markup.inline_keyboard for b in row]


async def test_the_menu_says_which_way_the_guard_is_set(db_pool):
    """A toggle whose button does not say its state is a coin flip: the only
    way to find out would be to press it and watch what the bot stops doing."""
    bot = _bot()

    await admin.handle_command(db_pool, bot, _Msg(chat_id=-501))

    labels = _button_labels(bot.send_message.await_args.kwargs["reply_markup"])
    assert "Только по теме: вкл" in labels


async def test_pressing_it_flips_the_setting_and_the_label(db_pool):
    bot = _bot()
    query = _Query(admin._GUARD, chat_id=-502)

    await admin.handle_callback(db_pool, bot, query)

    assert await settings.get_topic_guard(db_pool, -502) == settings.TOPIC_GUARD_OFF
    labels = _button_labels(query.edit_message_text.await_args.kwargs["reply_markup"])
    assert "Только по теме: выкл" in labels

    await admin.handle_callback(db_pool, bot, _Query(admin._GUARD, chat_id=-502))

    assert await settings.get_topic_guard(db_pool, -502) == settings.TOPIC_GUARD_STRICT


async def test_two_administrators_on_one_open_menu_do_not_cancel_out(db_pool):
    """The new value is read from storage, not carried in the button. Sent in
    the payload, the second press would re-apply what the first person saw
    and the setting would sit still while two people pressed it."""
    bot = _bot()
    await admin.handle_callback(db_pool, bot, _Query(admin._GUARD, chat_id=-503, user_id=7))
    await admin.handle_callback(db_pool, bot, _Query(admin._GUARD, chat_id=-503, user_id=8))

    assert await settings.get_topic_guard(db_pool, -503) == settings.TOPIC_GUARD_STRICT


async def test_anyone_but_the_owner_cannot_flip_it(db_pool):
    bot = _bot(status="administrator")

    await admin.handle_callback(
        db_pool, bot, _Query(admin._GUARD, chat_id=-504, user_id=7)
    )

    assert await settings.get_topic_guard(db_pool, -504) == settings.TOPIC_GUARD_STRICT
