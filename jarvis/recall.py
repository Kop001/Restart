"""Память разговоров: о чём говорили раньше.

Между короткой историей и списком фактов зияла дыра. История в промпте живёт
сорок ходов и обрезается; фактами становится только то, что Джарвис счёл
достойным записи. Всё остальное — вопрос, заданный неделю назад, и ответ на
него — исчезало без следа, хотя человек это помнит и рассчитывает, что помнят
и ему.

Здесь каждая пара «реплика — ответ» откладывается на диск и ищется теми же
основами слов, что и факты. Модель для этого не нужна: ни чтобы записать,
ни чтобы найти. Как и у хроники, у этой памяти есть срок жизни — журнал
разговоров без срока превращается в досье.
"""

from __future__ import annotations

import threading
import time
import uuid
from pathlib import Path

# Читаем и пишем теми же функциями, что и память: они закрывают файл
# правами 600 ещё до подмены, и заводить вторую такую пару незачем.
from .memory import _read, _write
from .morph import score

# Длинные реплики режем: память о разговоре — это о чём говорили, а не
# стенограмма. Целиком хранить незачем, а искать по обрезку не хуже.
LIMIT = 600

# Ниже этого совпадения эпизод сам в промпт не полезет: одно случайное слово
# притащило бы в разговор что попало, и Джарвис отвечал бы не на то. Когда
# ищут нарочно — инструментом, — хватает и одного совпадения: спросили же.
MIN_SCORE = 2


class Recall:
    """Разговоры, которые уже выпали из истории, но не из памяти."""

    def __init__(self, path: Path, keep_days: int = 90, persist: bool = True) -> None:
        self.path = path
        self.keep_days = keep_days
        self.persist = persist
        self.episodes: list[dict] = _read(path, []) if persist else []
        # Пишут и основной диалог, и фоновые исполнители.
        self._lock = threading.Lock()

    # --- запись ---

    def add(self, asked: str, answered: str, source: str = "диалог") -> dict | None:
        """Откладывает обмен репликами. Пустое не пишет."""
        asked, answered = asked.strip(), answered.strip()
        if not asked or not answered:
            return None

        episode = {
            "id": uuid.uuid4().hex[:8],
            "at": time.time(),
            "asked": _cut(asked),
            "answered": _cut(answered),
            "source": source,
        }
        with self._lock:
            self.episodes.append(episode)
            self._forget_old_locked()
            self._save_locked()
        return episode

    # --- чтение ---

    def search(self, query: str, count: int = 3, min_score: int = 1) -> list[dict]:
        """Ранжирует прошлые разговоры по совпадению слов.

        Сначала — где совпало больше; при равном счёте новое впереди старого:
        человек чаще спрашивает про недавнее.

        Args:
            query: О чём вспомнить.
            count: Сколько разговоров вернуть.
            min_score: Со скольких совпавших слов считать разговор найденным.
        """
        with self._lock:
            weighted = [
                (score(query, e["asked"] + " " + e["answered"]), e) for e in self.episodes
            ]
        ranked = sorted(
            (pair for pair in weighted if pair[0] >= min_score),
            key=lambda pair: (-pair[0], -pair[1]["at"]),
        )
        return [episode for _, episode in ranked[:count]]

    def recent(self, count: int = 10) -> list[dict]:
        with self._lock:
            return list(reversed(self.episodes[-count:]))

    def as_prompt(self, query: str, count: int = 2) -> str:
        """Кусок системного промпта: что уже обсуждали по этому поводу.

        Пусто, если ничего не совпало, — тогда и блока в промпте не будет.
        """
        found = self.search(query, count, min_score=MIN_SCORE)
        if not found:
            return ""
        lines = [
            f"- {when(e['at'])}: спросили «{_short(e['asked'])}» — "
            f"ответил «{_short(e['answered'])}»"
            for e in reversed(found)
        ]
        return (
            "Об этом уже говорили раньше (не пересказывай без нужды, "
            "просто помни):\n" + "\n".join(lines)
        )

    def __len__(self) -> int:
        with self._lock:
            return len(self.episodes)

    # --- служебное ---

    def clear(self) -> None:
        with self._lock:
            self.episodes = []
            self._save_locked()

    def _forget_old_locked(self) -> None:
        if not self.keep_days:
            return
        edge = time.time() - self.keep_days * 86400
        self.episodes = [e for e in self.episodes if e["at"] >= edge]

    def _save_locked(self) -> None:
        if not self.persist:
            return
        _write(self.path, self.episodes)


def when(at: float) -> str:
    """Человеческая давность: «вчера», «неделю назад», дата."""
    days = (time.time() - at) / 86400
    if days < 1:
        return "сегодня"
    if days < 2:
        return "вчера"
    if days < 7:
        return f"{int(days)} дн. назад"
    if days < 31:
        return f"{max(1, int(days / 7))} нед. назад"
    return time.strftime("%Y-%m-%d", time.localtime(at))


def _cut(text: str) -> str:
    return text if len(text) <= LIMIT else text[:LIMIT].rstrip() + "…"


def _short(text: str, limit: int = 200) -> str:
    """Для промпта берём ещё короче: важно о чём, а не дословно."""
    flat = " ".join(text.split())
    return flat if len(flat) <= limit else flat[:limit].rstrip() + "…"
