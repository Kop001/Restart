"""Нарезка ответа на фразы для озвучки на ходу.

Синтезатор говорит кусками, и кусок должен быть законченной мыслью: оборвать
на середине слова хуже, чем подождать. Поэтому копим текст и отдаём его, как
только фраза закончилась.

Заодно решаем задачу, которой не видно на бумаге: у длинного ответа первая
фраза уходит в синтез, пока модель ещё дописывает остальное.
"""

from __future__ import annotations

import re
from collections.abc import Callable

# Конец фразы: точка, вопрос, восклицание, многоточие или перевод строки.
_END = re.compile(r"[.!?…]+[\s»\"']*|\n+")

# Сокращения, после которых точка не заканчивает фразу.
ABBREVIATIONS = ("т.е", "т.к", "т.д", "т.п", "др", "напр", "см", "стр", "рис")


class Phrases:
    """Собирает поток текста и отдаёт законченные фразы."""

    def __init__(self, speak: Callable[[str], None], min_length: int = 12) -> None:
        self.speak = speak
        self.min_length = min_length
        self._buffer = ""

    def feed(self, piece: str) -> None:
        self._buffer += piece
        while True:
            phrase, rest = _split(self._buffer, self.min_length)
            if phrase is None:
                return
            self._buffer = rest
            self.speak(phrase)

    def flush(self) -> None:
        """Договаривает остаток — его конца уже не будет."""
        tail = self._buffer.strip()
        self._buffer = ""
        if tail:
            self.speak(tail)


def _split(text: str, min_length: int) -> tuple[str | None, str]:
    """Отрезает первую законченную фразу. None — фраза ещё не кончилась."""
    for match in _END.finditer(text):
        end = match.end()
        head = text[:end].strip()
        # Слишком короткое не отдаём: «Да.» лучше произнести вместе
        # со следующей фразой, чем отдельным вздохом.
        if len(head) < min_length:
            continue
        if _is_abbreviation(text[: match.start() + 1]):
            continue
        return head, text[end:]
    return None, text


def _is_abbreviation(upto: str) -> bool:
    tail = re.split(r"[\s(]", upto)[-1].rstrip(".").lower()
    return tail in ABBREVIATIONS
