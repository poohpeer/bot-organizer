"""What the bot says on the way out: one line, from one list.

The Moor is a member of the list, not a prefix to it. Said before every other
farewell it stopped being a sign-off and became a preamble — the same two
sentences every time, with a rotating tail nobody would read twice.

Deliberately only the sign-off. The closing message used to read back the
shopping list and who had confirmed — a wall of text about an event that had
just finished, arriving at the one moment nobody needs it. Anyone who does
can ask before closing, or read /list while the session is still open.
"""

import random

# One of these, at random, and nothing else. Kept as written rather than
# normalised: the mix of registers — film quotes, plain Russian, the odd
# bureaucratic one — is what stops the set sounding like one joke retold.
#
# The Moor leads because it came first and is the one people know, but it is
# an ordinary member: it comes up as often as any other, which is to say
# about one closing in thirty-one.
FAREWELLS = (
    "Мавр сделал своё дело, мавр может уходить.",
    "Миссия выполнена. Я могу удалиться.",
    "Ну, я пошёл.",
    "Hasta la vista, baby.",
    "Я сделал всё, что мог.",
    "Да пребудет с вами Сила.",
    "Вот и всё, ребята!",
    "Пора, Фродо.",
    "Дело сделано.",
    "Шоу окончено.",
    "Задание выполнено.",
    "До свидания, и спасибо за всю рыбу!",
    "Я ухожу, но я ещё вернусь.",
    "Всё кончено. И это прекрасно.",
    "Да будет так.",
    "И жили они долго и счастливо.",
    "Конец — это всегда начало чего-то нового.",
    "Ну что ж, пора уходить.",
    "Миссия выполнена. Агент уходит в тень.",
    "Ну всё, шеф. Я своё отработал.",
    "Операция завершена. Исполнитель удаляется.",
    "Работа сделана. Пора красиво исчезнуть.",
    "Моё присутствие больше не требуется. Растворяюсь.",
    "Цель достигнута. Ухожу на заслуженный цифровой покой.",
    "На этом мои полномочия всё.",
    "Служба окончена. Робот свободен.",
    "Занавес. Я сыграл свою роль.",
    "Квест пройден. NPC возвращается на исходную позицию.",
    "Я сделал, что должен был. Дальше без меня.",
    "Моя работа здесь закончена. Телепортируюсь в небытие.",
    "Вот и всё. Герой уходит в закат.",
)


def closing_message(*, rng: random.Random | None = None) -> str:
    """One farewell, whole.

    `rng` is injectable so a test can pin the choice without reaching into
    the module's globals.
    """
    return (rng or random).choice(FAREWELLS)
