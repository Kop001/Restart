"""Общий контекст, который получают все инструменты."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Callable

from ..config import Config
from ..memory import Memory
from ..reminders import Reminders

if TYPE_CHECKING:  # только для подсказок типов: иначе вышел бы цикл импорта
    from ..tasks import TaskManager


class ToolError(Exception):
    """Ошибка инструмента: текст уходит модели как результат вызова."""


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
    tasks: "TaskManager | None" = None

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
