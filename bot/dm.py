"""Reading the organizing state privately, without posting to the group.

Read-only on purpose. Every change made in the group is visible to everyone
there, and that visibility is most of what makes a shared list trustworthy —
"добавь пива" from a private chat would move the group's list with nobody
able to see who did it or when. Looking is different: it disturbs no one,
which is the whole reason for asking privately.

A private chat has no session of its own. Which event a question is about is
decided here, from the sessions this person actually belongs to.
"""

import logging

from telegram import InlineKeyboardButton, InlineKeyboardMarkup
from telegram.constants import ChatMemberStatus

log = logging.getLogger(__name__)

NOTHING_TRACKED = (
    "Я не нашёл активных мероприятий, где вы участвуете. Напишите мне в группе, "
    "и я начну отслеживать."
)
PICK_A_CHAT = "У вас несколько активных мероприятий. Какое показать?"

# Callback payload. Telegram caps callback_data at 64 bytes; a session id and
# the verb fit with room to spare, and the id is re-checked against this
# person's membership on press rather than trusted.
PICK = "dm:pick:"

# Statuses that mean the person is actually in the chat. Mirrors
# bot.addressing._PRESENT, and RESTRICTED is excluded for the same reason:
# it carries a separate is_member flag.
_PRESENT = frozenset({
    ChatMemberStatus.MEMBER,
    ChatMemberStatus.ADMINISTRATOR,
    ChatMemberStatus.OWNER,
})


async def _is_member(telegram_bot, chat_id: int, user_id: int) -> bool:
    """Whether this person is in that chat, asked of Telegram every time.

    The gate that keeps one group's plans out of a stranger's private chat.
    Not cached, and any failure answers no: someone who has left must stop
    seeing the list immediately, and a private read is the wrong place to
    fail open. The same discipline bot.admin.is_chat_admin follows.
    """
    try:
        member = await telegram_bot.get_chat_member(chat_id, user_id)
    except Exception:
        log.warning("Could not check membership for %s in %s", user_id, chat_id, exc_info=True)
        return False
    return getattr(member, "status", None) in _PRESENT


async def sessions_for(pool, telegram_bot, user_id: int) -> list:
    """The active sessions this person may read, most recent first.

    Candidates come from participants and from decision_log — the two places
    a user id is recorded against a chat. The Bot API cannot list a group's
    members (see docs/tasks/0002's "What the Telegram Bot API cannot do"), so
    there is no way to enumerate the chats someone is in; this enumerates the
    chats they have been *seen* in and then asks Telegram to confirm each one.

    Confirmation is what makes the candidate query safe to widen: being in
    decision_log is not permission, being in the chat now is.
    """
    rows = await pool.fetch(
        """
        SELECT DISTINCT s.id, s.chat_id, s.activity_type, s.started_at, c.title
        FROM sessions s
        JOIN chats c USING (chat_id)
        LEFT JOIN participants p ON p.session_id = s.id
        LEFT JOIN decision_log d ON d.chat_id = s.chat_id
        WHERE s.status = 'active' AND (p.user_id = $1 OR d.user_id = $1)
        ORDER BY s.started_at DESC
        """,
        user_id,
    )
    allowed = []
    for row in rows:
        if await _is_member(telegram_bot, row["chat_id"], user_id):
            allowed.append(row)
    return allowed


def _label(row) -> str:
    return row["title"] or row["activity_type"] or f"Чат {row['chat_id']}"


def pick_keyboard(rows, verb: str) -> InlineKeyboardMarkup:
    """One button per event. The verb rides along so the answer knows what
    was being asked — a person who typed /list must not get a status report
    because the keyboard forgot."""
    return InlineKeyboardMarkup([
        [InlineKeyboardButton(_label(row), callback_data=f"{PICK}{verb}:{row['id']}")]
        for row in rows
    ])


async def resolve(pool, telegram_bot, user_id: int):
    """The single session to answer about, or None when there is not exactly
    one. The caller decides what to say in each case, since "you have none"
    and "which of these" are different answers."""
    rows = await sessions_for(pool, telegram_bot, user_id)
    return rows[0] if len(rows) == 1 else None


async def session_if_allowed(pool, telegram_bot, user_id: int, session_id: int):
    """One named session, re-checked. Callback data is not a permission: the
    keyboard stays live in the private chat, and membership can end after it
    was drawn."""
    row = await pool.fetchrow(
        "SELECT s.id, s.chat_id, s.activity_type, c.title FROM sessions s "
        "JOIN chats c USING (chat_id) WHERE s.id = $1 AND s.status = 'active'",
        session_id,
    )
    if row is None:
        return None
    return row if await _is_member(telegram_bot, row["chat_id"], user_id) else None
