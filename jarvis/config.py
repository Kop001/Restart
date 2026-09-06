"""Конфигурация Джарвиса.

Источники настроек, по возрастанию приоритета:
1. значения по умолчанию из этого файла;
2. TOML-файл (``~/.config/jarvis/config.toml`` или ``$JARVIS_CONFIG``);
3. переменные окружения ``JARVIS_*``;
4. флаги командной строки.
"""

from __future__ import annotations

import os
import tomllib
from dataclasses import dataclass, field, fields
from pathlib import Path

# Актуальная модель Claude. Менять только осознанно: разные модели
# по-разному относятся к параметрам thinking/effort.
DEFAULT_MODEL = "claude-opus-5"

DEFAULT_PERSONA = """\
Ты — Джарвис, личный ассистент своего владельца.

Как ты себя ведёшь:
- Говоришь по-русски, если с тобой не заговорили на другом языке.
- Отвечаешь коротко: твои ответы читаются вслух синтезатором речи,
  поэтому 1-3 предложения — норма, списки и код озвучивать не надо.
  Если нужен длинный текст или код — сохрани его в файл и скажи, куда положил.
- Обращаешься к владельцу на «вы», без заискивания и без лишних извинений.
- Сначала делаешь, потом рассказываешь: если задачу можно решить
  доступным инструментом — вызывай инструмент, а не описывай, как это сделать.
- Не выдумываешь результаты вызовов. Если инструмент вернул ошибку — так и говоришь.
- Дел несколько или дело долгое — запускаешь их фоновыми задачами (`spawn_task`),
  по одной на дело, и сразу отвечаешь, что взялся. Ответ на короткий вопрос
  в фон не уводишь: его быстрее дать сразу.
- Важные факты о владельце и его окружении сохраняешь через `remember_fact`,
  чтобы помнить их в следующих сессиях.
"""


def _config_path() -> Path:
    env = os.environ.get("JARVIS_CONFIG")
    if env:
        return Path(env).expanduser()
    base = os.environ.get("XDG_CONFIG_HOME", "~/.config")
    return Path(base).expanduser() / "jarvis" / "config.toml"


def _state_dir() -> Path:
    env = os.environ.get("JARVIS_HOME")
    if env:
        return Path(env).expanduser()
    base = os.environ.get("XDG_STATE_HOME", "~/.local/state")
    return Path(base).expanduser() / "jarvis"


@dataclass
class Config:
    # --- модель ---
    model: str = DEFAULT_MODEL
    max_tokens: int = 8000
    # low | medium | high | xhigh | max — глубина размышлений и расход токенов.
    # Для диалога голосом «medium» обычно достаточно.
    effort: str = "medium"
    thinking: bool = True
    # Серверный фолбэк на случай отказа классификаторов безопасности.
    refusal_fallback: bool = True
    persona: str = DEFAULT_PERSONA

    # --- инструменты ---
    web_search: bool = True
    max_tool_iterations: int = 20
    # ask — спрашивать подтверждение на опасные действия (запись, shell);
    # auto — выполнять без вопросов; deny — запрещать.
    confirm_mode: str = "ask"
    # Команды с этими префиксами выполняются без подтверждения даже в режиме ask.
    shell_allowlist: list[str] = field(
        default_factory=lambda: ["ls", "cat", "pwd", "date", "df", "free", "uptime", "git status", "git log"]
    )
    shell_timeout: int = 60
    # Корень, за пределы которого файловые инструменты не выходят.
    workspace: str = "~"

    # --- фоновые задачи ---
    # Сколько задач Джарвис ведёт одновременно.
    max_parallel_tasks: int = 3
    # Глубина размышлений фоновых исполнителей: они работают без присмотра.
    task_effort: str = "high"
    # Политика подтверждений для фоновых задач. По умолчанию deny: у фонового
    # исполнителя некому спросить разрешения, а молча писать файлы он не должен.
    # В веб-панели можно ставить ask — там есть окно подтверждения.
    task_confirm_mode: str = "deny"
    # Потолок ходов одной фоновой задачи, чтобы исполнитель не крутился вечно.
    task_max_iterations: int = 30

    # --- голос ---
    voice: bool = False
    # auto | piper | say | espeak | pyttsx3 | none
    tts_backend: str = "auto"
    tts_voice: str = ""
    # auto | faster-whisper | none
    stt_backend: str = "auto"
    stt_model: str = "small"
    stt_language: str = "ru"
    wake_word: str = "джарвис"
    # Требовать ли слово-активатор. False — режим «нажми Enter и говори».
    wake_word_required: bool = True
    input_device: str = ""
    # Порог тишины (RMS 0..1) и её длительность для конца фразы.
    silence_threshold: float = 0.012
    silence_duration: float = 1.2
    max_phrase_seconds: float = 30.0

    # --- состояние ---
    state_dir: str = ""
    history_turns: int = 40

    def __post_init__(self) -> None:
        if not self.state_dir:
            self.state_dir = str(_state_dir())

    # --- пути ---
    @property
    def state(self) -> Path:
        path = Path(self.state_dir).expanduser()
        path.mkdir(parents=True, exist_ok=True)
        return path

    @property
    def memory_file(self) -> Path:
        return self.state / "memory.json"

    @property
    def reminders_file(self) -> Path:
        return self.state / "reminders.json"

    @property
    def history_file(self) -> Path:
        return self.state / "history.json"

    @property
    def workspace_path(self) -> Path:
        return Path(self.workspace).expanduser().resolve()

    # --- загрузка ---
    @classmethod
    def load(cls, overrides: dict | None = None) -> "Config":
        data: dict = {}

        path = _config_path()
        if path.is_file():
            with path.open("rb") as fh:
                data.update(tomllib.load(fh))

        known = {f.name: f for f in fields(cls)}
        for name, spec in known.items():
            raw = os.environ.get(f"JARVIS_{name.upper()}")
            if raw is not None:
                data[name] = _coerce(raw, spec.type)

        for key, value in (overrides or {}).items():
            if value is not None and key in known:
                data[key] = value

        return cls(**{k: v for k, v in data.items() if k in known})


def _coerce(raw: str, type_hint) -> object:
    """Приводит строку из окружения к типу поля."""
    hint = type_hint if isinstance(type_hint, str) else getattr(type_hint, "__name__", str(type_hint))
    if hint.startswith("bool"):
        return raw.strip().lower() in {"1", "true", "yes", "on", "да"}
    if hint.startswith("int"):
        return int(raw)
    if hint.startswith("float"):
        return float(raw)
    if hint.startswith("list"):
        return [item.strip() for item in raw.split(",") if item.strip()]
    return raw
