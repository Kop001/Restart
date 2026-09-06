"""Долговременная память и история диалога.

Память — это плоский список фактов в JSON. Факты подмешиваются в системный
промпт при каждом запуске, поэтому Джарвис помнит их между сессиями, а не
только внутри одного разговора.
"""

from __future__ import annotations

import json
import re
import threading
import time
import uuid
from pathlib import Path

from .morph import score


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


# Личные данные — только для владельца. Каталог 700, файлы 600: иначе их
# читает любой пользователь машины, а в них переписка и факты о человеке.
DIR_MODE = 0o700
FILE_MODE = 0o600


def secure(path: Path) -> Path:
    """Закрывает файл или каталог от посторонних. На Windows — тихо ничего."""
    try:
        path.chmod(DIR_MODE if path.is_dir() else FILE_MODE)
    except OSError:
        pass
    return path


def _write(path: Path, payload) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    secure(path.parent)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    # Права ставим до подмены: между записью и chmod не должно быть окна,
    # в котором файл лежит открытым.
    secure(tmp)
    tmp.replace(path)


class Memory:
    """Факты о владельце, которые Джарвис помнит между запусками.

    Факт не удаляется, когда устаревает: он помечается заменённым и перестаёт
    попадать в промпт. Так видно, что чем вытеснено, и ошибочную замену можно
    заметить — а молчаливое удаление заметить нельзя.
    """

    def __init__(self, path: Path, persist: bool = True) -> None:
        self.path = path
        self.persist = persist
        self.facts: list[dict] = [_upgrade(f) for f in _read(path, [])]
        # Память одна на всех: основной диалог и фоновые исполнители
        # пишут в неё из разных потоков.
        self._lock = threading.Lock()

    # --- запись ---

    def add(
        self,
        text: str,
        tag: str = "general",
        replaces: str = "",
        importance: str = "normal",
        source: str = "диалог",
    ) -> dict:
        """Записывает факт, при необходимости заменяя устаревший.

        Возвращает записанный факт. Если такой уже есть слово в слово —
        возвращает существующий, не плодя копию.
        """
        text = text.strip()
        if not text:
            raise ValueError("пустой факт")

        with self._lock:
            twin = self._find_twin_locked(text)
            if twin is not None:
                return twin

            fact = {
                "id": uuid.uuid4().hex[:8],
                "text": text,
                "tag": (tag.strip() or "general"),
                "importance": importance if importance in IMPORTANCE else "normal",
                "created_at": time.strftime("%Y-%m-%d %H:%M"),
                "source": source,
                "uses": 0,
                "last_used": None,
                "superseded_by": None,
                "superseded_at": None,
            }
            self.facts.append(fact)
            if replaces:
                self._supersede_locked(replaces, fact["id"])
            self._save_locked()
        return fact

    def forget(self, fact_id: str) -> bool:
        """Удаляет факт насовсем — в отличие от замены."""
        with self._lock:
            before = len(self.facts)
            self.facts = [f for f in self.facts if f["id"] != fact_id]
            if len(self.facts) == before:
                return False
            self._save_locked()
        return True

    # --- чтение ---

    def active(self) -> list[dict]:
        """Факты, которые ещё в силе."""
        with self._lock:
            return [f for f in self.facts if not f["superseded_by"]]

    def stale(self) -> list[dict]:
        """Заменённые факты — их видно в панели, но в работу они не идут."""
        with self._lock:
            return [f for f in self.facts if f["superseded_by"]]

    def search(
        self,
        query: str = "",
        tag: str = "",
        include_stale: bool = False,
        count: bool = False,
    ) -> list[dict]:
        """Ищет факты по смыслу слов. `count` отмечает, что они пригодились.

        Сравниваем основы, а не буквы: записано «место работы», спросили
        «где я работаю» — должно найтись. Ответ отсортирован: сначала то,
        где совпало больше слов.
        """
        query = query.strip()
        tag = tag.lower().strip()

        with self._lock:
            candidates = [
                f for f in self.facts
                if (include_stale or not f["superseded_by"])
                and (not tag or f["tag"].lower() == tag)
            ]
            weighted = [(score(query, f["text"] + " " + f["tag"]), f) for f in candidates]
            found = [f for weight, f in sorted(weighted, key=lambda p: -p[0]) if weight]

            if count and found:
                stamp = time.strftime("%Y-%m-%d %H:%M")
                for fact in found:
                    fact["uses"] += 1
                    fact["last_used"] = stamp
                self._save_locked()
            return found

    def similar(self, text: str, tag: str = "", limit: int = 3) -> list[dict]:
        """Действующие факты, похожие на новый.

        Нужна при записи: если новое сведение отменяет старое, Джарвис должен
        это заметить — а не заметив, оставит в памяти два противоречащих.
        """
        found = [f for f in self.search(text, tag) if _key(f["text"]) != _key(text)]
        return found[:limit]

    def search_all(self) -> list[dict]:
        with self._lock:
            return list(self.facts)

    def as_prompt(self, limit: int = 100) -> str:
        """Действующие факты в виде куска системного промпта."""
        facts = self.active()
        if not facts:
            return ""
        lines = [
            f"- [{f['id']}] ({f['tag']}{'!' if f['importance'] == 'high' else ''}) {f['text']}"
            for f in facts[-limit:]
        ]
        return "Что ты уже знаешь о владельце:\n" + "\n".join(lines)

    # --- служебное ---

    def find_twin(self, text: str) -> dict | None:
        """Ищет действующий факт, который слово в слово повторяет новый.

        Нужна вызывающему коду: по одному возвращённому факту не понять,
        записали его сейчас или он лежал раньше.
        """
        with self._lock:
            return self._find_twin_locked(text)

    def _find_twin_locked(self, text: str) -> dict | None:
        key = _key(text)
        for fact in self.facts:
            if not fact["superseded_by"] and _key(fact["text"]) == key:
                return fact
        return None

    def _supersede_locked(self, old_id: str, new_id: str) -> bool:
        for fact in self.facts:
            if fact["id"] == old_id and not fact["superseded_by"]:
                fact["superseded_by"] = new_id
                fact["superseded_at"] = time.strftime("%Y-%m-%d %H:%M")
                return True
        return False

    def save(self) -> None:
        with self._lock:
            self._save_locked()

    def _save_locked(self) -> None:
        # В приватном режиме новые факты живут только в памяти процесса.
        if not self.persist:
            return
        _write(self.path, self.facts)


IMPORTANCE = ("low", "normal", "high")

# Поля, которых не было в первой версии памяти. Старые файлы читаются как есть,
# недостающее добирается значениями по умолчанию.
DEFAULTS = {
    "importance": "normal",
    "uses": 0,
    "superseded_by": None,
    "superseded_at": None,
    "source": "диалог",
    "last_used": None,
}


def _upgrade(fact: dict) -> dict:
    for name, value in DEFAULTS.items():
        fact.setdefault(name, value)
    return fact


def _key(text: str) -> str:
    """Приводит фразу к виду, в котором сравниваются почти-дубликаты."""
    return " ".join(re.sub(r"[^\w\s]", " ", text.lower()).split())


class History:
    """История сообщений с обрезкой по числу ходов.

    Обрезка идёт по границам ходов: срез не должен начинаться с ответа на
    вызов инструмента, иначе API отвергнет запрос.
    """

    def __init__(self, path: Path, max_turns: int = 40, persist: bool = True) -> None:
        self.path = path
        self.max_turns = max_turns
        # В приватном режиме прошлое с диска не поднимаем и новое не пишем:
        # разговор живёт только пока запущен процесс.
        self.persist = persist
        self.messages: list[dict] = _read(path, []) if persist else []

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
        if not self.persist:
            return
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
