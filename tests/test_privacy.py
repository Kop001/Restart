"""Приватность: права на файлы, стирание и режим «не запоминай»."""

from __future__ import annotations

import json
import platform

import pytest

from jarvis.config import Config
from jarvis.memory import History, Memory
from jarvis.privacy import KINDS, describe, survey, wipe
from jarvis.recall import Recall
from jarvis.reminders import Reminders

# На Windows права POSIX не работают — проверять там нечего.
posix_only = pytest.mark.skipif(
    platform.system() == "Windows", reason="права POSIX есть только не в Windows"
)


@pytest.fixture
def config(tmp_path, monkeypatch):
    monkeypatch.setenv("JARVIS_HOME", str(tmp_path / "state"))
    return Config.load({"workspace": str(tmp_path)})


@pytest.fixture
def filled(config):
    """Джарвис, которому уже есть что помнить."""
    memory = Memory(config.memory_file)
    memory.add("владелец лечится от такого-то", "личное")
    memory.add("живёт по такому-то адресу", "личное")

    history = History(config.history_file, 40)
    history.extend([{"role": "user", "content": "пароль от сейфа 1234"}])

    recall = Recall(config.recall_file)
    recall.add("а что мы решили по деньгам", "решили подождать до весны")

    reminders = Reminders(config.reminders_file)
    reminders.add("приём у врача", "через 1 час")

    tasks = config.state / "tasks"
    tasks.mkdir(parents=True, exist_ok=True)
    (tasks / "abc123.json").write_text("[]", encoding="utf-8")
    return config


# --- права ---

@posix_only
def test_personal_files_are_owner_only(filled):
    """Переписку и память не должен читать другой пользователь машины."""
    assert filled.state.stat().st_mode & 0o777 == 0o700
    for path in (filled.memory_file, filled.history_file,
                 filled.recall_file, filled.reminders_file):
        assert path.stat().st_mode & 0o777 == 0o600, path.name


@posix_only
def test_temporary_file_is_closed_too(config):
    """Промежуточный файл не должен на миг оказаться открытым."""
    memory = Memory(config.memory_file)
    memory.add("секрет", "личное")
    leftovers = list(config.state.glob("*.tmp"))
    assert leftovers == [], "временные файлы не должны оставаться"


# --- стирание ---

def test_survey_counts_everything(filled):
    counts = survey(filled)
    assert counts == {"history": 1, "recall": 1, "memory": 2, "tasks": 1, "reminders": 1}


def test_wipe_removes_files_not_just_contents(filled):
    removed = wipe(filled)

    assert removed == {"history": 1, "recall": 1, "memory": 2, "tasks": 1, "reminders": 1}
    for path in (filled.memory_file, filled.history_file,
                 filled.recall_file, filled.reminders_file):
        assert not path.exists(), f"{path.name} должен быть удалён, а не опустошён"
    assert not (filled.state / "tasks").exists()
    assert survey(filled) == dict.fromkeys(KINDS, 0)


def test_wipe_can_be_narrowed(filled):
    wipe(filled, ("history",))

    assert not filled.history_file.exists()
    assert filled.memory_file.exists(), "память не просили стирать"
    assert survey(filled)["memory"] == 2


def test_wipe_rejects_unknown_kind(filled):
    with pytest.raises(ValueError):
        wipe(filled, ("всё-подряд",))


def test_describe_is_readable():
    assert "переписка: 3" in describe({"history": 3})


# --- режим «не запоминай» ---

def test_private_mode_writes_nothing(config):
    memory = Memory(config.memory_file, persist=False)
    history = History(config.history_file, 40, persist=False)

    memory.add("это не должно попасть на диск", "личное")
    history.extend([{"role": "user", "content": "и это тоже"}])

    assert not config.memory_file.exists()
    assert not config.history_file.exists()
    # Но внутри сессии всё помнится.
    assert memory.search("не должно")
    assert len(history.messages) == 1


def test_private_mode_does_not_read_old_history(config):
    History(config.history_file, 40).extend([{"role": "user", "content": "прошлый разговор"}])
    assert json.loads(config.history_file.read_text(encoding="utf-8"))

    private = History(config.history_file, 40, persist=False)

    assert private.messages == [], "прошлое не должно подниматься с диска"


def test_agent_honours_private_mode(config, monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-stub")
    from jarvis.agent import Agent

    config.private_mode = True
    agent = Agent(config, say=lambda text: None)

    assert agent.history.persist is False
    assert agent.memory.persist is False
    assert "не сохранится" in agent.system_prompt()[-1]["text"]
