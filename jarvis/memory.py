"""Долговременная память и история диалога.

Память — это плоский список фактов в JSON. Факты подмешиваются в системный
промпт при каждом запуске, поэтому Джарвис помнит их между сессиями, а не
только внутри одного разговора.
"""

from __future__ import annotations

import json
import time
import uuid
from pathlib import Path


def to_plain(value):
    """Разворачивает объекты SDK (pydantic-модели) в обычные dict/list.

    В историю попадают блоки ответа модели как объекты SDK; на диск они
    должны лечь простым JSON, иначе следующий запуск не поднимет историю.
    """
    if isinstance(value, list):
        return [to_plain(item) for item in value]
    if isinstance(value, dict):
        return {k: to_plain(v) for k, v in value.items() if v is not None}
    dump = getattr(value, "model_dump", None)
    if callable(dump):
        return to_plain(dump(mode="json", exclude_none=True))
    return value


def _read(path: Path, default):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return default


def _write(path: Path, payload) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(path)


class Memory:
    """Факты о владельце, которые Джарвис помнит между запусками."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self.facts: list[dict] = _read(path, [])

    def add(self, text: str, tag: str = "general") -> dict:
        fact = {
            "id": uuid.uuid4().hex[:8],
            "text": text.strip(),
            "tag": tag.strip() or "general",
            "created_at": time.strftime("%Y-%m-%d %H:%M"),
        }
        self.facts.append(fact)
        self.save()
        return fact

    def search(self, query: str = "", tag: str = "") -> list[dict]:
        query = query.lower().strip()
        tag = tag.lower().strip()
        result = self.facts
        if tag:
            result = [f for f in result if f["tag"].lower() == tag]
        if query:
            result = [f for f in result if query in f["text"].lower()]
        return result

    def forget(self, fact_id: str) -> bool:
        before = len(self.facts)
        self.facts = [f for f in self.facts if f["id"] != fact_id]
        if len(self.facts) != before:
            self.save()
            return True
        return False

    def as_prompt(self, limit: int = 100) -> str:
        """Факты в виде куска системного промпта."""
        if not self.facts:
            return ""
        lines = [f"- [{f['id']}] ({f['tag']}) {f['text']}" for f in self.facts[-limit:]]
        return "Что ты уже знаешь о владельце:\n" + "\n".join(lines)

    def save(self) -> None:
        _write(self.path, self.facts)


class History:
    """История сообщений с обрезкой по числу ходов.

    Обрезка идёт по границам ходов: срез не должен начинаться с ответа на
    вызов инструмента, иначе API отвергнет запрос.
    """

    def __init__(self, path: Path, max_turns: int = 40) -> None:
        self.path = path
        self.max_turns = max_turns
        self.messages: list[dict] = _read(path, [])

    def extend(self, messages: list[dict]) -> None:
        self.messages.extend(to_plain(messages))
        self.trim()
        self.save()

    def trim(self) -> None:
        if len(self.messages) <= self.max_turns:
            return
        cut = len(self.messages) - self.max_turns
        # Двигаем срез вперёд, пока он не встанет на «чистое» сообщение
        # пользователя (не на tool_result).
        while cut < len(self.messages) and not _is_clean_user_turn(self.messages[cut]):
            cut += 1
        self.messages = self.messages[cut:]

    def clear(self) -> None:
        self.messages = []
        self.save()

    def save(self) -> None:
        _write(self.path, self.messages)


def _is_clean_user_turn(message: dict) -> bool:
    if message.get("role") != "user":
        return False
    content = message.get("content")
    if isinstance(content, str):
        return True
    return not any(
        isinstance(block, dict) and block.get("type") == "tool_result" for block in content or []
    )
