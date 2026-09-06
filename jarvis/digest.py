"""Сводка: что произошло, пока вы не смотрели.

Помощник, который только отвечает на вопросы, — справочник. Помощник, который
сам рассказывает, что случилось, — уже помощник. Сводка собирается из хроники
без обращения к модели: считать и группировать умеет и обычный код, а лишний
запрос — это лишнее личное наружу.
"""

from __future__ import annotations

import time
from collections import Counter

from .events import SILENT, SPEAK, EventLog

# Сколько строк подробностей показывать, прежде чем схлопнуть в счёт.
DETAIL_LIMIT = 5


def since(log: EventLog, hours: float = 24.0) -> str:
    """Собирает сводку за последние часы. Пустая строка — рассказывать нечего."""
    edge = time.time() - hours * 3600
    events = [e for e in log.recent(500) if e.at >= edge]
    if not events:
        return ""

    said = [e for e in events if e.outcome == SPEAK and e.source not in ("человек", "джарвис")]
    talked = sum(1 for e in events if e.source == "человек")
    silent = [e for e in events if e.outcome == SILENT]
    to_model = sum(1 for e in events if e.sent_to_model)

    lines: list[str] = []

    if said:
        lines.append("Говорил вам:")
        lines += [f"  · {e.text}" for e in said[:DETAIL_LIMIT]]
        if len(said) > DETAIL_LIMIT:
            lines.append(f"  · и ещё {len(said) - DETAIL_LIMIT}")

    if silent:
        by_source = Counter(e.source for e in silent)
        parts = ", ".join(f"{source} — {count}" for source, count in by_source.most_common(3))
        lines.append(f"Промолчал: {len(silent)} ({parts}).")

    if talked:
        lines.append(f"Вы обращались {talked} раз{_ending(talked)}.")

    lines.append(
        f"Наружу уходило {to_model} из {len(events)}: остальное разобрано на месте."
    )
    return "\n".join(lines)


def _ending(count: int) -> str:
    if count % 10 == 1 and count % 100 != 11:
        return ""
    return "а" if count % 10 in (2, 3, 4) and count % 100 not in (12, 13, 14) else ""
