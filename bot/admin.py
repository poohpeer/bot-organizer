"""/admin — the configuration menu.

One setting so far: which models this chat tries, and in what order.

Restricted to chat administrators. The chain decides how every message in
the group is answered, so leaving it open to any member would let one person
change the bot's behaviour for everyone else without their knowing.
"""

import logging

from telegram import InlineKeyboardButton, InlineKeyboardMarkup

import bot.ai.client as ai_client
import bot.settings as settings

log = logging.getLogger(__name__)

_NOT_AN_ADMIN = "Настройки доступны администраторам чата."
_MENU_TITLE = "Настройки бота. Что меняем?"
_CHAIN_TITLE = (
    "Порядок моделей. Нажимайте их в том порядке, в каком бот должен их "
    "звать: первое нажатие — первый номер. Нажать ещё раз — убрать.\n\n"
)
# Not a warning. An empty selection is a valid state — bot/settings.py falls
# back to the deployment default — and the ▲▼ menu this replaced could not
# express "start over" at all without disabling eight models one by one.
_CHAIN_EMPTY = "Ничего не выбрано — бот идёт по умолчанию:\n{chain}"
_SAVED = "Сохранено."

# 1..8 today; the list is short and Telegram's own digits are the clearest
# way to say "this one is third" on a button.
_NUMBERS = ("1\ufe0f\u20e3", "2\ufe0f\u20e3", "3\ufe0f\u20e3", "4\ufe0f\u20e3", "5\ufe0f\u20e3",
            "6\ufe0f\u20e3", "7\ufe0f\u20e3", "8\ufe0f\u20e3", "9\ufe0f\u20e3")


def _mark(index: int | None) -> str:
    """The badge in front of a model: its place in the chain, or an empty box."""
    if index is None:
        return "\u25fb\ufe0f"
    return _NUMBERS[index] if index < len(_NUMBERS) else f"{index + 1}."


# Callback payloads. Short on purpose: Telegram caps callback_data at 64
# bytes, and a model name like "openai/gpt-oss-120b" plus a verb would not
# fit reliably — so a position is sent instead of a name.
_MENU = "adm:menu"
_CHAIN = "adm:chain"
_PICK = "adm:pick:"
_RESET = "adm:reset"
_CLOSE = "adm:close"


async def is_chat_admin(telegram_bot, chat_id: int, user_id: int | None) -> bool:
    """Whether this person may change the chat's settings.

    Asked of Telegram every time rather than cached: an administrator who has
    been demoted should stop being one immediately, and a cache would decide
    otherwise. Any failure answers no — a settings menu is the wrong place to
    fail open.
    """
    if user_id is None:
        return False
    try:
        member = await telegram_bot.get_chat_member(chat_id, user_id)
    except Exception:
        log.warning("Could not check admin status for %s in %s", user_id, chat_id, exc_info=True)
        return False
    return getattr(member, "status", None) in {"creator", "administrator"}


def _menu_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("Порядок моделей", callback_data=_CHAIN)],
        [InlineKeyboardButton("Закрыть", callback_data=_CLOSE)],
    ])


def _shown_order(chosen: list[str]) -> list[str]:
    """Chosen first, in their chain order, then everything still unpicked.

    The numbered ones float to the top so the chain reads down the screen,
    and the rest stay listed so turning one on never means remembering it
    exists — a menu that hides what you did not pick cannot undo itself.
    """
    available = list(ai_client.PROXY_MODELS)
    return [m for m in chosen if m in available] + [m for m in available if m not in chosen]


def chain_view(chosen: list[str]) -> tuple[str, InlineKeyboardMarkup]:
    """The chain as text plus its controls.

    One button per model, full width: the names are long ("openai/gpt-oss-120b")
    and the three-button rows this replaced left no room to read them.
    """
    order = _shown_order(chosen)

    lines, rows = [], []
    for position, model in enumerate(order):
        index = chosen.index(model) if model in chosen else None
        badge = _mark(index)
        lines.append(f"{badge} {model}")
        rows.append([InlineKeyboardButton(f"{badge} {model}", callback_data=f"{_PICK}{position}")])

    rows.append([
        InlineKeyboardButton("Очистить", callback_data=_RESET),
        InlineKeyboardButton("Назад", callback_data=_MENU),
    ])
    if chosen:
        body = _CHAIN_TITLE + "\n".join(lines)
    else:
        body = _CHAIN_TITLE + _CHAIN_EMPTY.format(
            chain=" \u2192 ".join(ai_client.PROXY_MODELS)
        ) + "\n\n" + "\n".join(lines)
    return body, InlineKeyboardMarkup(rows)


def pick(chosen: list[str], position: int) -> list[str]:
    """One button press against the shown order.

    A model not yet in the chain goes on the end, taking the next number; one
    already in it comes out, and everything after it moves up. That is the
    whole interaction — the ▲▼ pair this replaced needed seven presses to
    lift the last model to the front, and this needs one press per model you
    actually want.
    """
    order = _shown_order(chosen)
    if not 0 <= position < len(order):
        return list(chosen)

    model = order[position]
    if model in chosen:
        return [m for m in chosen if m != model]
    return list(chosen) + [model]


async def handle_command(pool, telegram_bot, message) -> None:
    """/admin — open the menu, for an administrator."""
    chat_id = message.chat.id
    user_id = message.from_user.id if message.from_user else None
    if not await is_chat_admin(telegram_bot, chat_id, user_id):
        await telegram_bot.send_message(chat_id=chat_id, text=_NOT_AN_ADMIN)
        return
    await telegram_bot.send_message(
        chat_id=chat_id, text=_MENU_TITLE, reply_markup=_menu_keyboard()
    )


async def handle_callback(pool, telegram_bot, query) -> None:
    """A button press on an open menu.

    Admin status is re-checked here and not only when the menu was opened: a
    keyboard stays live in the chat afterwards, so anyone could press it, and
    whoever opened it may since have been demoted.
    """
    data = query.data or ""
    chat_id = query.message.chat.id if query.message else None
    user_id = query.from_user.id if query.from_user else None

    if chat_id is None or not await is_chat_admin(telegram_bot, chat_id, user_id):
        await query.answer(_NOT_AN_ADMIN, show_alert=True)
        return

    if data == _CLOSE:
        await query.answer()
        await query.edit_message_text("Закрыто.")
        return

    if data == _MENU:
        await query.answer()
        await query.edit_message_text(_MENU_TITLE, reply_markup=_menu_keyboard())
        return

    if data == _RESET:
        # Deleting the row is both "clear the selection" and "back to the
        # default" — with the fallback in settings they are the same state,
        # so the menu offers one button rather than two that look different
        # and are not.
        await settings.reset_provider_chain(pool, chat_id)
        ai_client.forget_chat(chat_id)
        await query.answer(_SAVED)
        await _show_chain(pool, chat_id, query)
        return

    if data == _CHAIN:
        await query.answer()
        await _show_chain(pool, chat_id, query)
        return

    if data.startswith(_PICK):
        try:
            position = int(data[len(_PICK):])
        except ValueError:
            await query.answer()
            return
        chosen = await settings.get_stored_chain(pool, chat_id) or []
        updated = pick(chosen, position)
        if updated != chosen:
            await settings.set_provider_chain(pool, chat_id, updated, updated_by=user_id)
            # Dropped rather than edited: the sticky index the chain carries
            # refers to positions that just moved.
            ai_client.forget_chat(chat_id)
        await query.answer()
        await _show_chain(pool, chat_id, query)
        return

    await query.answer()


async def _show_chain(pool, chat_id: int, query) -> None:
    # The stored selection, not the effective chain: get_provider_chain
    # substitutes the default for an empty one, which would draw eight
    # numbered models over a chat that has picked none.
    body, keyboard = chain_view(await settings.get_stored_chain(pool, chat_id) or [])
    try:
        await query.edit_message_text(body, reply_markup=keyboard)
    except Exception as e:
        # Telegram refuses an edit that changes nothing. That happens on any
        # no-op press — the top item moved up, say — and is not worth
        # reporting to the person who pressed it.
        if "not modified" not in str(e).lower():
            raise
