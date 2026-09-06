"""Режимы работы: текстовый диалог и голосовой цикл со словом-активатором.

Ключевая особенность обоих режимов — владелец сам разрешает каждое опасное
действие, кто бы его ни просил. Поэтому основной ход диалога выполняется не
в главном потоке, а рядом: главный поток остаётся консолью оператора и
свободен, чтобы задать вопрос и принять ответ, пока диалог ждёт.
"""

from __future__ import annotations

import collections
import difflib
import queue
import re
import sys
import threading

from .agent import Agent
from .approvals import ApprovalQueue
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

Когда Джарвис или фоновая задача просят разрешение, ответьте «да» или «нет» —
следующая реплика будет понята как ответ на запрос.
"""

YES = {"y", "yes", "д", "да", "ага", "давай", "ок", "окей", "разрешаю", "можно", "конечно"}
NO = {"n", "no", "н", "нет", "не", "отмена", "стоп", "запрещаю", "нельзя"}

_EOF = object()


def parse_answer(text: str) -> bool | None:
    """Понимает «да»/«нет» в свободной форме. None — ответ неразборчив."""
    words = re.findall(r"\w+", text.lower(), flags=re.UNICODE)
    for word in words:
        if word in YES:
            return True
        if word in NO:
            return False
    return None


class Session:
    """Обвязка вокруг агента: голос, разрешения, напоминания, команды."""

    def __init__(self, config: Config) -> None:
        self.config = config
        self.speaker = Speaker(config.tts_backend, config.tts_voice) if config.voice else None
        # Одна очередь на всех: и диалог, и фоновые исполнители спрашивают сюда.
        self.approvals = ApprovalQueue(timeout=config.approval_timeout)
        self.agent = Agent(
            config,
            confirm=lambda action: self.approvals.request(action, "диалог"),
            say=self.say,
        )
        self.scheduler = ReminderScheduler(self.agent.reminders, self._fire_reminder)
        self.tasks = TaskManager(
            config,
            self.agent.memory,
            self.agent.reminders,
            client=self.agent.client,
            confirm=lambda action: self.approvals.request(action, "фоновая задача"),
            on_done=self._task_done,
        )
        self.agent.attach_tasks(self.tasks)
        self._turn: threading.Thread | None = None
        self._asked: str | None = None
        # Реплики, сказанные раньше, чем их стало кому принять.
        self._held: collections.deque[str] = collections.deque()
        self._quitting = False

    # --- вывод ---

    def say(self, text: str) -> None:
        if self.speaker is not None:
            self.speaker.say(text)
        else:
            print(f"🤖 {text}", flush=True)

    def _fire_reminder(self, item: dict) -> None:
        self.say(f"Напоминание: {item['text']}")

    def _task_done(self, task) -> None:
        if task.status == "cancelled":
            return
        if task.error:
            self.say(f"Задача «{task.title}» сорвалась: {task.error}")
        else:
            self.say(f"Задача «{task.title}» готова. {task.result}")

    # --- разрешения ---

    def pending_prompt(self) -> str | None:
        """Текст запроса, который сейчас ждёт ответа, если он новый."""
        pending = self.approvals.pending()
        if not pending:
            self._asked = None
            return None
        first = pending[0]
        if first["id"] == self._asked:
            return None
        self._asked = first["id"]
        return f"{first['source'].capitalize()} просит разрешение: {first['action']}"

    def answer_approval(self, text: str) -> bool:
        """Пробует истолковать реплику как ответ на запрос разрешения."""
        decision = parse_answer(text)
        if decision is None:
            self.say("Не понял. Ответьте «да» или «нет».")
            return True
        self.approvals.resolve_first(decision)
        self._asked = None
        print("   " + ("разрешено" if decision else "отклонено"))
        return True

    # --- команды ---

    def handle_command(self, text: str) -> bool:
        """Обрабатывает служебную команду. Возвращает True, если пора выходить."""
        action = COMMANDS[text.strip().lower()]
        if action == "quit":
            return self.request_quit()
        if action == "help":
            print(HELP)
        elif action == "reset":
            self.agent.reset()
            print("Разговор сброшен.")
        elif action == "memory":
            print(self.agent.memory.as_prompt() or "Память пуста.")
        elif action == "reminders":
            items = self.agent.reminders.pending()
            lines = "\n".join(f"[{i['id']}] {i['due']} — {i['text']}" for i in items)
            print(lines or "Напоминаний нет.")
        elif action == "tasks":
            items = self.tasks.list()
            print("\n".join(task.summary() for task in items) or "Фоновых задач нет.")
        return False

    def request_quit(self) -> bool:
        """Просит завершить работу. True — можно выходить прямо сейчас.

        Незаконченный ход и висящий запрос разрешения не бросаем: иначе
        владелец теряет и ответ, и уже начатое действие.
        """
        self._quitting = True
        if self.busy or self.approvals.pending() or self._held:
            print("   Джарвис ещё работает — выйду, как закончит.")
            return False
        return True

    @property
    def ready_to_quit(self) -> bool:
        return (
            self._quitting
            and not self.busy
            and not self.approvals.pending()
            and not self._held
        )

    # --- ход диалога ---

    def respond(self, text: str) -> str:
        answer = self.agent.ask(text)
        self.say(answer)
        return answer

    def start_turn(self, text: str) -> None:
        """Запускает ход диалога рядом, не занимая консоль.

        Иначе разрешение, которое запросит сам диалог, было бы некому выдать.
        """
        self._turn = threading.Thread(target=self._run_turn, args=(text,), daemon=True)
        self._turn.start()

    def _run_turn(self, text: str) -> None:
        try:
            self.respond(text)
        except Exception as exc:
            self.say(f"Сорвалось: {type(exc).__name__}: {exc}")

    @property
    def busy(self) -> bool:
        return self._turn is not None and self._turn.is_alive()

    def dispatch(self, text: str) -> bool:
        """Разбирает реплику владельца. Возвращает True, если пора выходить.

        Порядок разбора важен: сначала ожидающий запрос разрешения, потом
        служебные команды, и только потом — обычная реплика Джарвису.
        """
        text = text.strip()
        if not text:
            return False
        if self.approvals.pending():
            self.answer_approval(text)
            return False
        if text.lower() in COMMANDS:
            return self.handle_command(text)
        if self.busy:
            # Ход ещё идёт. Реплику не выбрасываем: она может оказаться
            # ответом на разрешение, которое диалог вот-вот запросит.
            self._held.append(text)
            print("   Джарвис занят — реплика подождёт.")
            return False
        self.start_turn(text)
        return False

    def flush_held(self) -> bool:
        """Отдаёт отложенную реплику, как только есть кому её принять."""
        if self._held and (self.approvals.pending() or not self.busy):
            return self.dispatch(self._held.popleft())
        return False

    # --- режимы ---

    def run_text(self) -> None:
        self.scheduler.start()
        print("Джарвис на связи. /помощь — список команд.\n")
        lines: queue.Queue = queue.Queue()
        threading.Thread(target=_read_stdin, args=(lines,), daemon=True).start()
        try:
            while True:
                prompt = self.pending_prompt()
                if prompt:
                    print(f"\n⚠️  {prompt}\n   Разрешить? [да/нет]")
                if self.flush_held() or self.ready_to_quit:
                    break
                try:
                    line = lines.get(timeout=0.15)
                except queue.Empty:
                    continue
                if line is _EOF:
                    if self.request_quit():
                        break
                    continue
                if self.dispatch(line):
                    break
        finally:
            print()
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
                # Запрос разрешения произносится вслух: у микрофона экрана нет.
                prompt = self.pending_prompt()
                if prompt:
                    self.say(f"{prompt}. Разрешить?")
                self.flush_held()

                audio = mic.record_phrase()
                if audio is None:
                    continue
                text = stt.transcribe(audio)
                if not text:
                    continue
                print(f"👤 {text}")

                # На запрос разрешения отвечают без слова-активатора:
                # «Джарвис, да» — это лишнее, когда он сам только что спросил.
                if self.approvals.pending():
                    self.answer_approval(text)
                    continue

                if self.config.wake_word_required:
                    stripped = strip_wake_word(text, wake)
                    if stripped is None:
                        continue
                    text = stripped or "Слушаю вас."
                self.dispatch(text)
        except KeyboardInterrupt:
            print()
        finally:
            self.scheduler.stop()
            self.tasks.shutdown()


def _read_stdin(lines: queue.Queue) -> None:
    """Читает stdin в отдельном потоке, чтобы главный не блокировался на вводе."""
    while True:
        try:
            lines.put(input("👤 "))
        except (EOFError, KeyboardInterrupt):
            lines.put(_EOF)
            return


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
