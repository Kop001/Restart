"""Разбор события моделью — для того, что не разобрали правила.

Запрос нарочно крошечный: приметы события и четыре возможных исхода. Тело
письма или содержимое файла сюда не попадает — только то, что уже лежит
в событии, а событие несёт приметы.
"""

from __future__ import annotations

import anthropic

from .config import Config
from .events import OUTCOMES, SILENT, Event

PROMPT = """\
Ты — часть личного ассистента. Тебе показывают одно происшествие, и надо решить,
что с ним делать. Владелец не хочет, чтобы его дёргали по мелочам, но не хочет
и пропустить важное.

Ответь одним словом из четырёх:
промолчать — записать и не беспокоить;
запомнить — стоит запомнить о владельце;
сделать — требует действия ассистента;
сказать — стоит того, чтобы прервать человека.

После слова — короткое пояснение, одна фраза.
"""


def make_triage(client: anthropic.Anthropic, config: Config):
    """Возвращает функцию разбора для EventWorker."""

    def triage(event: Event) -> tuple[str, str]:
        описание = (
            f"Источник: {event.source}\n"
            f"Что: {event.kind}\n"
            f"Текст: {event.text}\n"
            f"Важность по мнению источника: {event.weight} из 100"
        )
        try:
            response = client.messages.create(
                model=config.model,
                max_tokens=200,
                # Дёшево: решение простое, размышлять тут не над чем.
                output_config={"effort": "low"},
                system=PROMPT,
                messages=[{"role": "user", "content": описание}],
            )
        except Exception as exc:
            # Не смогли спросить — молчим. Ошибка разбора не повод будить человека.
            return SILENT, f"модель недоступна: {type(exc).__name__}"

        return _parse(_text(response))

    return triage


def _text(response) -> str:
    return " ".join(b.text for b in response.content if getattr(b, "type", None) == "text")


def _parse(answer: str) -> tuple[str, str]:
    """Достаёт исход из ответа. Непонятный ответ — повод промолчать, а не гадать."""
    lowered = answer.lower()
    for outcome in OUTCOMES:
        if lowered.startswith(outcome) or f"\n{outcome}" in lowered:
            note = answer[len(outcome):].strip(" —-:.\n") or "без пояснения"
            return outcome, note[:200]
    return SILENT, f"непонятный ответ модели: {answer[:80]}"
