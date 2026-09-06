"""Режимы работы: текстовый диалог и голосовой цикл со словом-активатором."""

from __future__ import annotations

import difflib
import re
import sys

from .agent import Agent
from .config import Config
from .reminders import ReminderScheduler
from .tasks import TaskManager
from .voice import Microphone, Speaker, Transcriber

COMMANDS = {
    "/выход": "quit", "/quit": "quit", "/exit": "quit",
    "/сброс": "reset", "/reset": "reset",
    "/память": "memory", "/memory": "memory",
    "/напоминания": "reminders", "/reminders": "reminders",
    "/задачи": "tasks", "/tasks": "tasks",
    "/помощь": "help", "/help": "help",
}

HELP = """\
Команды:
  /помощь        — этот список
  /память        — что Джарвис помнит о вас
  /напоминания   — активные напоминания
  /задачи        — фоновые задачи и их состояние
  /сброс         — забыть текущий разговор (память останется)
  /выход         — завершить работу
"""


class Session:
    """Обвязка вокруг агента: голос, подтверждения, напоминания, команды."""

    def __init__(self, config: Config) -> None:
        self.config = config
        self.speaker = Speaker(config.tts_backend, config.tts_voice) if config.voice else None
        self.agent = Agent(config, confirm=self._confirm, say=self.say)
        self.scheduler = ReminderScheduler(self.agent.reminders, self._fire_reminder)
        self.tasks = TaskManager(
            config,
            self.agent.memory,
            self.agent.reminders,
            client=self.agent.client,
            confirm=self._confirm_background,
            on_done=self._task_done,
        )
        self.agent.attach_tasks(self.tasks)

    # --- вывод и подтверждения ---

    def say(self, text: str) -> None:
        if self.speaker is not None:
            self.speaker.say(text)
        else:
            print(f"🤖 {text}", flush=True)

    def _confirm(self, action: str) -> bool:
        print(f"\n⚠️  Джарвис просит разрешение: {action}")
        try:
            answer = input("Разрешить? [y/N] ").strip().lower()
        except (EOFError, KeyboardInterrupt):
            return False
        return answer in {"y", "yes", "д", "да"}

    def _confirm_background(self, action: str) -> bool:
        """Подтверждение для фоновой задачи.

        В терминале спросить нельзя: ввод занят основным диалогом. Поэтому
        отказываем и говорим об этом вслух — в панели такое окно есть.
        """
        print(f"\n⚠️  Фоновая задача просила разрешение: {action}")
        print("   В терминале подтвердить нельзя — отклонено. Используйте `jarvis panel`.")
        return False

    def _fire_reminder(self, item: dict) -> None:
        self.say(f"Напоминание: {item['text']}")

    def _task_done(self, task) -> None:
        if task.status == "cancelled":
            return
        if task.error:
            self.say(f"Задача «{task.title}» сорвалась: {task.error}")
        else:
            self.say(f"Задача «{task.title}» готова. {task.result}")

    # --- команды ---

    def handle_command(self, text: str) -> bool:
        """Обрабатывает служебную команду. Возвращает True, если пора выходить."""
        action = COMMANDS[text.strip().lower()]
        if action == "quit":
            return True
        if action == "help":
            print(HELP)
        elif action == "reset":
            self.agent.reset()
            print("Разговор сброшен.")
        elif action == "memory":
            print(self.agent.memory.as_prompt() or "Память пуста.")
        elif action == "reminders":
            items = self.agent.reminders.pending()
            print("\n".join(f"[{i['id']}] {i['due']} — {i['text']}" for i in items) or "Напоминаний нет.")
        elif action == "tasks":
            items = self.tasks.list()
            print("\n".join(task.summary() for task in items) or "Фоновых задач нет.")
        return False

    def respond(self, text: str) -> str:
        answer = self.agent.ask(text)
        self.say(answer)
        return answer

    # --- режимы ---

    def run_text(self) -> None:
        self.scheduler.start()
        print("Джарвис на связи. /помощь — список команд.\n")
        try:
            while True:
                try:
                    user_input = input("👤 ").strip()
                except (EOFError, KeyboardInterrupt):
                    print()
                    break
                if not user_input:
                    continue
                if user_input.lower() in COMMANDS:
                    if self.handle_command(user_input):
                        break
                    continue
                self.respond(user_input)
        finally:
            self.scheduler.stop()
            self.tasks.shutdown()

    def run_voice(self) -> None:
        mic = Microphone(
            device=self.config.input_device,
            silence_threshold=self.config.silence_threshold,
            silence_duration=self.config.silence_duration,
            max_phrase_seconds=self.config.max_phrase_seconds,
        )
        stt = Transcriber(self.config.stt_backend, self.config.stt_model, self.config.stt_language)

        if not mic.available or not stt.available:
            print(
                "Голосовой режим недоступен (нужны sounddevice, numpy, faster-whisper) — "
                "перехожу в текстовый.",
                file=sys.stderr,
            )
            self.run_text()
            return

        self.scheduler.start()
        wake = self.config.wake_word.lower()
        if self.config.wake_word_required:
            print(f"Слушаю. Скажите «{self.config.wake_word}», чтобы обратиться. Ctrl+C — выход.\n")
        else:
            print("Слушаю непрерывно. Ctrl+C — выход.\n")

        try:
            while True:
                audio = mic.record_phrase()
                if audio is None:
                    continue
                text = stt.transcribe(audio)
                if not text:
                    continue
                print(f"👤 {text}")

                if self.config.wake_word_required:
                    stripped = strip_wake_word(text, wake)
                    if stripped is None:
                        continue
                    text = stripped or "Слушаю вас."
                self.respond(text)
        except KeyboardInterrupt:
            print()
        finally:
            self.scheduler.stop()
            self.tasks.shutdown()


def strip_wake_word(text: str, wake: str) -> str | None:
    """Убирает слово-активатор из начала фразы.

    Возвращает остаток фразы, либо None, если активатора в ней нет.
    Распознавание речи склоняет и коверкает имя, поэтому сравниваем нечётко.
    """
    words = re.findall(r"\w+", text.lower(), flags=re.UNICODE)
    if not words:
        return None
    for index, word in enumerate(words[:3]):
        if difflib.SequenceMatcher(None, word, wake).ratio() >= 0.72:
            rest = re.split(r"\w+", text, maxsplit=index + 1, flags=re.UNICODE)[-1]
            return rest.strip(" ,.!?—-")
    return None
