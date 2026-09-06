"""Общий контекст, который получают все инструменты."""

from __future__ import annotations

import functools
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

from ..config import Config
from ..memory import Memory
from ..reminders import Reminders

if TYPE_CHECKING:  # только для подсказок типов: иначе вышел бы цикл импорта
    from ..tasks import TaskManager


class ToolError(Exception):
    """Ожидаемая осечка инструмента: отказ владельца, нет файла, плохой формат."""


def guard(func):
    """Превращает ожидаемую осечку в обычный текстовый результат.

    Отказ владельца — нормальный исход, а не сбой программы. Без этого SDK
    печатает в консоль трассировку на каждое «нет», пугая владельца тем,
    что всё сломалось.
    """

    @functools.wraps(func)
    def wrapper(*args, **kwargs):
        try:
            return func(*args, **kwargs)
        except ToolError as exc:
            return f"не вышло: {exc}"
        except OSError as exc:
            return f"ошибка файловой системы: {exc}"

    return wrapper


@dataclass
class ToolContext:
    config: Config
    memory: Memory
    reminders: Reminders
    # Спросить у владельца разрешение на опасное действие.
    confirm: Callable[[str], bool]
    # Сказать что-то вслух (или напечатать, если голос выключен).
    say: Callable[[str], None]
    # Менеджер фоновых задач. None — у фоновых исполнителей: они не плодят
    # собственных исполнителей, иначе один запрос развернётся в лавину.
    tasks: TaskManager | None = None

    def ensure_allowed(self, action: str) -> None:
        """Проверяет политику подтверждений перед опасным действием."""
        mode = self.config.confirm_mode
        if mode == "auto":
            return
        if mode == "deny":
            raise ToolError(f"действие запрещено политикой (confirm_mode=deny): {action}")
        if not self.confirm(action):
            raise ToolError(f"владелец отклонил действие: {action}")

    def resolve_path(self, raw: str) -> Path:
        """Разрешает путь и не выпускает за пределы рабочего каталога."""
        root = self.config.workspace_path
        path = Path(raw).expanduser()
        path = path if path.is_absolute() else root / path
        path = path.resolve()
        if root not in path.parents and path != root:
            raise ToolError(f"путь {path} вне рабочего каталога {root}")
        return path
