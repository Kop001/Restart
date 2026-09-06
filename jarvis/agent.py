"""Ядро Джарвиса: цикл «запрос — инструменты — ответ» поверх Claude API."""

from __future__ import annotations

import platform
from datetime import datetime
from pathlib import Path

import anthropic

from .config import Config
from .memory import History, Memory
from .reminders import Reminders
from .tools import ToolContext, build_tools

# Серверный фолбэк на случай отказа модели по соображениям безопасности:
# запрос автоматически уходит на подходящую запасную модель.
FALLBACK_BETA = "server-side-fallback-2026-07-01"
MAX_PAUSE_RESTARTS = 5


class Agent:
    """Один разговор с Джарвисом, переживающий перезапуски процесса."""

    def __init__(
        self,
        config: Config,
        *,
        client: anthropic.Anthropic | None = None,
        confirm=None,
        say=None,
        memory: Memory | None = None,
        reminders: Reminders | None = None,
        history_path: Path | None = None,
        with_task_tools: bool = True,
        should_stop=None,
        source: str = "диалог",
        log=None,
    ) -> None:
        self.config = config
        self.client = client or anthropic.Anthropic()
        # Память и напоминания общие: фоновые исполнители получают их снаружи,
        # чтобы всё, что узнал один, знали остальные.
        keep = not config.private_mode
        self.memory = memory or Memory(config.memory_file, persist=keep)
        self.reminders = reminders or Reminders(config.reminders_file)
        self.history = History(
            history_path or config.history_file, config.history_turns, persist=keep
        )
        self.should_stop = should_stop or (lambda: False)
        self.ctx = ToolContext(
            config=config,
            memory=self.memory,
            reminders=self.reminders,
            confirm=confirm or (lambda action: False),
            say=say or (lambda text: print(text)),
            source=source,
            log=log,
        )
        self.tools = build_tools(self.ctx, with_tasks=with_task_tools)
        # Порядок фиксирован — он входит в префикс запроса, а кэш префиксный.
        self.tool_specs = [t if isinstance(t, dict) else t.to_dict() for t in self.tools]
        self.callable_tools = {
            t.to_dict()["name"]: t for t in self.tools if not isinstance(t, dict)
        }
        self._extra_params_supported = True
        # Счётчики для панели «Аналитика».
        self.stats: dict[str, int] = {
            "requests": 0,
            "turns": 0,
            "input_tokens": 0,
            "output_tokens": 0,
            "cache_read_tokens": 0,
            "tool_calls": 0,
        }

    # --- системный промпт ---

    def system_prompt(self) -> list[dict]:
        """Системный промпт тремя блоками: от самого стабильного к самому изменчивому.

        Кэш промптов работает по префиксу, поэтому персона (она не меняется)
        идёт первой и помечается cache_control, а время — последним.
        """
        blocks: list[dict] = [
            {
                "type": "text",
                "text": self.config.persona,
                "cache_control": {"type": "ephemeral"},
            }
        ]
        facts = self.memory.as_prompt()
        if facts:
            blocks.append({"type": "text", "text": facts})
        blocks.append({"type": "text", "text": self._runtime_block()})
        return blocks

    def _runtime_block(self) -> str:
        pending = self.reminders.pending()
        lines = [
            "Текущая обстановка:",
            f"- время: {datetime.now().strftime('%Y-%m-%d %H:%M, %A')}",
            f"- машина: {platform.system()} {platform.release()}",
            f"- рабочий каталог: {self.config.workspace_path}",
            f"- голосовой режим: {'включён' if self.config.voice else 'выключен'}",
            f"- подтверждение опасных действий: {self.config.confirm_mode}",
            "- запись на диск: "
            + ("выключена, разговор не сохранится" if self.config.private_mode else "включена"),
        ]
        if pending:
            due = "; ".join(f"{i['due']} {i['text']}" for i in pending)
            lines.append(f"- активные напоминания: {due}")
        return "\n".join(lines)

    # --- основной цикл ---

    def ask(self, user_input: str, on_text=None) -> str:
        """Прогоняет реплику владельца через модель и возвращает текст ответа.

        Цикл вызова инструментов написан вручную, а не взят из SDK: готовый
        ранер не умеет отдавать ответ по мере готовности, а без этого Джарвис
        молчит всю долгую мысль и только потом выдаёт её целиком.

        Args:
            user_input: Что сказал владелец.
            on_text: Получает куски ответа по мере их появления — чтобы
                начать говорить с первой фразы, не дожидаясь конца.
        """
        messages = [*self.history.messages, {"role": "user", "content": user_input}]
        new_messages: list[dict] = [{"role": "user", "content": user_input}]

        last = None
        stopped = False
        pauses = 0

        for _ in range(self.config.max_tool_iterations):
            if self.should_stop():
                stopped = True
                break

            last = self._one_turn(messages, on_text)
            self._track(last)

            turn = {"role": "assistant", "content": last.content}
            messages.append(turn)
            new_messages.append(turn)

            if last.stop_reason == "pause_turn":
                # Серверный инструмент не уложился в ход: продолжаем без
                # новой реплики, история уже заканчивается ходом модели.
                pauses += 1
                if pauses > MAX_PAUSE_RESTARTS:
                    break
                continue

            if last.stop_reason != "tool_use":
                break

            results = self._run_tools(last)
            if results:
                answer = {"role": "user", "content": results}
                messages.append(answer)
                new_messages.append(answer)

        self.stats["turns"] += 1
        self.history.extend(new_messages)

        if stopped:
            return "задача остановлена по требованию"
        if last is None:
            return "не получил ответа от модели"
        if last.stop_reason == "refusal":
            reason = getattr(last.stop_details, "category", None)
            return f"я не могу ответить на это{f' ({reason})' if reason else ''}"
        return extract_text(last) or "готово"

    def _one_turn(self, messages: list[dict], on_text=None):
        """Один запрос к модели с потоковой выдачей текста."""
        params = self._params(messages)
        try:
            return self._stream(params, on_text, extra=True)
        except (TypeError, anthropic.BadRequestError):
            # Старый SDK или модель без серверных фолбэков — дальше без них.
            self._extra_params_supported = False
            return self._stream(params, on_text, extra=False)

    def _stream(self, params: dict, on_text, extra: bool):
        if extra and self._extra_params_supported and self.config.refusal_fallback:
            params = {**params, "betas": [FALLBACK_BETA], "fallbacks": "default"}
        with self.client.beta.messages.stream(**params) as stream:
            if on_text is not None:
                for piece in stream.text_stream:
                    on_text(piece)
            return stream.get_final_message()

    def _run_tools(self, message) -> list[dict]:
        """Выполняет вызванные инструменты и собирает ответы для модели.

        Серверные инструменты вроде веб-поиска выполняются на стороне
        Anthropic — их блоки сюда не попадают и выполнять нечего.
        """
        results: list[dict] = []
        for block in message.content:
            if getattr(block, "type", None) != "tool_use":
                continue
            tool = self.callable_tools.get(block.name)
            if tool is None:
                results.append(_tool_result(block.id, f"нет такого навыка: {block.name}", True))
                continue
            try:
                results.append(_tool_result(block.id, str(tool.call(block.input))))
            except Exception as exc:
                results.append(_tool_result(block.id, f"{type(exc).__name__}: {exc}", True))
        return results

    def _params(self, messages: list[dict]) -> dict:
        params: dict = {
            "model": self.config.model,
            "max_tokens": self.config.max_tokens,
            "system": self.system_prompt(),
            "messages": messages,
            "tools": self.tool_specs,
            "output_config": {"effort": self.config.effort},
        }
        if self.config.thinking:
            params["thinking"] = {"type": "adaptive"}
        return params

    def _track(self, message) -> None:
        """Копит расход токенов и число вызовов инструментов."""
        usage = getattr(message, "usage", None)
        self.stats["requests"] += 1
        if usage is not None:
            self.stats["input_tokens"] += getattr(usage, "input_tokens", 0) or 0
            self.stats["output_tokens"] += getattr(usage, "output_tokens", 0) or 0
            self.stats["cache_read_tokens"] += getattr(usage, "cache_read_input_tokens", 0) or 0
        self.stats["tool_calls"] += sum(
            1 for block in message.content if getattr(block, "type", None) == "tool_use"
        )

    # --- обслуживание ---

    def attach_tasks(self, manager) -> None:
        """Отдаёт агенту менеджер фоновых задач.

        Инструменты читают ctx.tasks в момент вызова, поэтому пересобирать
        их список (и ломать префиксный кэш) не нужно.
        """
        self.ctx.tasks = manager

    def reset(self) -> None:
        """Забывает текущий разговор, но не долговременную память."""
        self.history.clear()


def _tool_result(tool_use_id: str, content: str, is_error: bool = False) -> dict:
    result = {"type": "tool_result", "tool_use_id": tool_use_id, "content": content}
    if is_error:
        result["is_error"] = True
    return result


def extract_text(message) -> str:
    """Склеивает текстовые блоки ответа, пропуская thinking и вызовы инструментов."""
    parts = [block.text for block in message.content if getattr(block, "type", None) == "text"]
    return "\n".join(part.strip() for part in parts if part.strip()).strip()
