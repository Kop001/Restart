"""Управление личными данными: что стереть и что вовсе не писать на диск.

Джарвис знает о владельце много, и это знание должно управляться так же
просто, как накапливается: одной командой.
"""

from __future__ import annotations

import json
import shutil
from pathlib import Path

from .config import Config

# Что вообще можно стереть. Порядок — от самого чувствительного.
KINDS = ("history", "memory", "tasks", "reminders")

TITLES = {
    "history": "переписка",
    "memory": "память о вас",
    "tasks": "истории фоновых задач",
    "reminders": "напоминания",
}


def survey(config: Config) -> dict[str, int]:
    """Сколько записей лежит на диске по каждому виду данных."""
    return {
        "history": _count(config.history_file),
        "memory": _count(config.memory_file),
        "tasks": len(list((config.state / "tasks").glob("*.json"))),
        "reminders": _count(config.reminders_file),
    }


def wipe(config: Config, kinds: tuple[str, ...] = KINDS) -> dict[str, int]:
    """Стирает выбранные данные. Возвращает, сколько записей удалено.

    Стираем файлы целиком, а не содержимое: файл с пустым списком внутри
    оставляет на диске прежние байты до перезаписи.
    """
    before = survey(config)
    removed: dict[str, int] = {}

    for kind in kinds:
        if kind not in KINDS:
            raise ValueError(f"неизвестный вид данных: {kind}")
        if kind == "tasks":
            shutil.rmtree(config.state / "tasks", ignore_errors=True)
        else:
            _remove(getattr(config, f"{kind}_file"))
        removed[kind] = before[kind]

    return removed


def describe(removed: dict[str, int]) -> str:
    """Человеческий отчёт о том, что стёрли."""
    parts = [f"{TITLES[kind]}: {count}" for kind, count in removed.items()]
    return "стёрто — " + ", ".join(parts) if parts else "стирать было нечего"


def _count(path: Path) -> int:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return 0
    return len(data) if isinstance(data, list) else 0


def _remove(path: Path) -> None:
    try:
        path.unlink()
    except OSError:
        pass
