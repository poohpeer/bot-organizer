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
    "Порядок моделей. Бот идёт по списку сверху вниз и переходит к следующей, "
    "когда предыдущая недоступна.\n\n"
)
_CHAIN_EMPTY = "Все модели выключены — бот не сможет ответить. Включите хотя бы одну."
_SAVED = "Сохранено."

# Callback payloads. Short on purpose: Telegram caps callback_data at 64
# bytes, and a model name like "openai/gpt-oss-120b" plus a verb would not
# fit reliably — so a position is sent instead of a name.
_MENU = "adm:menu"
_CHAIN = "adm:chain"
_UP = "adm:up:"
_DOWN = "adm:down:"
_TOGGLE = "adm:tog:"
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


def chain_view(chosen: list[str]) -> tuple[str, InlineKeyboardMarkup]:
    """The chain as text plus its controls.

    Every model the deployment offers is listed, on or off, so turning one
    back on is possible from the same screen that turned it off — a menu that
    hides what you disabled cannot undo itself.
    """
    available = list(ai_client.PROXY_MODELS)
    order = [m for m in chosen if m in available] + [m for m in available if m not in chosen]

    lines, rows = [], []
    for position, model in enumerate(order):
        enabled = model in chosen
        mark = f"{chosen.index(model) + 1}." if enabled else "—"
        lines.append(f"{mark} {model}" + ("" if enabled else "  (выключена)"))
        rows.append([
            InlineKeyboardButton("▲", callback_data=f"{_UP}{position}"),
            InlineKeyboardButton("▼", callback_data=f"{_DOWN}{position}"),
            InlineKeyboardButton("✅" if enabled else "◻️", callback_data=f"{_TOGGLE}{position}"),
        ])

    rows.append([
        InlineKeyboardButton("Сбросить", callback_data=_RESET),
        InlineKeyboardButton("Назад", callback_data=_MENU),
    ])
    body = _CHAIN_TITLE + "\n".join(lines)
    if not chosen:
        body += "\n\n" + _CHAIN_EMPTY
    return body, InlineKeyboardMarkup(rows)


def apply_action(chosen: list[str], action: str, position: int) -> list[str]:
    """One button press against the shown order.

    Works on the shown order — enabled entries first, then disabled ones —
    because that is what the person is looking at when they press a button.
    Returns the new enabled list; a move that would fall off either end is a
    no-op rather than an error.
    """
    available = list(ai_client.PROXY_MODELS)
    order = [m for m in chosen if m in available] + [m for m in available if m not in chosen]
    if not 0 <= position < len(order):
        return list(chosen)

    model = order[position]
    if action == "tog":
        if model in chosen:
            return [m for m in chosen if m != model]
        return list(chosen) + [model]

    if model not in chosen:
        # Moving a disabled model would reorder something the chain never
        # reaches, which reads as nothing happening.
        return list(chosen)

    index = chosen.index(model)
    swap_with = index - 1 if action == "up" else index + 1
    if not 0 <= swap_with < len(chosen):
        return list(chosen)
    new = list(chosen)
    new[index], new[swap_with] = new[swap_with], new[index]
    return new


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
        await settings.reset_provider_chain(pool, chat_id)
        ai_client.forget_chat(chat_id)
        await query.answer(_SAVED)
        await _show_chain(pool, chat_id, query)
        return

    if data == _CHAIN:
        await query.answer()
        await _show_chain(pool, chat_id, query)
        return

    for prefix, action in ((_UP, "up"), (_DOWN, "down"), (_TOGGLE, "tog")):
        if data.startswith(prefix):
            try:
                position = int(data[len(prefix):])
            except ValueError:
                await query.answer()
                return
            chosen = await settings.get_provider_chain(pool, chat_id)
            updated = apply_action(chosen, action, position)
            if updated != chosen:
                await settings.set_provider_chain(pool, chat_id, updated, updated_by=user_id)
                # Dropped rather than edited: the sticky index the chain
                # carries refers to positions that just moved.
                ai_client.forget_chat(chat_id)
            await query.answer()
            await _show_chain(pool, chat_id, query)
            return

    await query.answer()


async def _show_chain(pool, chat_id: int, query) -> None:
    body, keyboard = chain_view(await settings.get_provider_chain(pool, chat_id))
    try:
        await query.edit_message_text(body, reply_markup=keyboard)
    except Exception as e:
        # Telegram refuses an edit that changes nothing. That happens on any
        # no-op press — the top item moved up, say — and is not worth
        # reporting to the person who pressed it.
        if "not modified" not in str(e).lower():
            raise
