"""Сводка: что Джарвис расскажет о времени, пока вы не смотрели."""

from __future__ import annotations

import time

import pytest

from jarvis.config import Config
from jarvis.digest import since
from jarvis.events import SILENT, SPEAK, EventLog
from jarvis.memory import Memory
from jarvis.reminders import Reminders
from jarvis.tools import build_tools
from jarvis.tools.context import ToolContext


@pytest.fixture
def config(tmp_path, monkeypatch):
    monkeypatch.setenv("JARVIS_HOME", str(tmp_path / "state"))
    return Config.load({"workspace": str(tmp_path), "confirm_mode": "auto"})


@pytest.fixture
def log(config):
    return EventLog(config.events_file, keep_days=30)


def put(log, source, kind, text, outcome, ago=60.0, model=False):
    event = log.emit(source, kind, text)
    event.at = time.time() - ago
    log.resolve(event, outcome, "модель" if model else "правило", sent_to_model=model)
    return event


def test_empty_chronicle_says_nothing(log):
    assert since(log) == ""


def test_digest_names_what_was_said_out_loud(log):
    put(log, "время", "напоминание", "Через сорок минут созвон", SPEAK)
    put(log, "задача", "задача-готова", "Отчёт готов", SPEAK)

    text = since(log)

    assert "Говорил вам:" in text
    assert "Через сорок минут созвон" in text
    assert "Отчёт готов" in text


def test_own_replies_are_not_news(log):
    """Свои же ответы владельцу — не то, что стоит пересказывать."""
    put(log, "человек", "реплика", "привет", SPEAK)
    put(log, "джарвис", "ответ", "здравствуйте", SPEAK)

    text = since(log)

    assert "Говорил вам:" not in text
    assert "обращались 1 раз" in text


def test_silence_is_counted_not_listed(log):
    for i in range(12):
        put(log, "почта", "письмо", f"рассылка {i}", SILENT)

    text = since(log)

    assert "Промолчал: 12" in text
    assert "рассылка 5" not in text, "молчание пересказывать незачем"


def test_long_list_is_folded(log):
    for i in range(9):
        put(log, "задача", "задача-готова", f"задача {i} готова", SPEAK)

    text = since(log)

    assert "и ещё 4" in text


def test_digest_shows_what_left_the_machine(log):
    put(log, "почта", "письмо", "рассылка", SILENT)
    put(log, "почта", "письмо", "письмо от коллеги", SPEAK, model=True)

    assert "Наружу уходило 1 из 2" in since(log)


def test_old_events_are_out_of_the_window(log):
    put(log, "задача", "задача-готова", "вчерашнее", SPEAK, ago=40 * 3600)
    put(log, "задача", "задача-готова", "свежее", SPEAK, ago=60)

    text = since(log, hours=24)

    assert "свежее" in text
    assert "вчерашнее" not in text


def test_tool_tells_the_story(config, log):
    ctx = ToolContext(
        config=config,
        memory=Memory(config.memory_file),
        reminders=Reminders(config.reminders_file),
        confirm=lambda action: True,
        say=lambda text: None,
        log=log,
    )
    tools = {t.to_dict()["name"]: t for t in build_tools(ctx) if not isinstance(t, dict)}
    put(log, "задача", "задача-готова", "Отчёт готов", SPEAK)

    assert "Отчёт готов" in tools["what_happened"].call({})


def test_tool_is_honest_when_there_is_no_chronicle(config):
    ctx = ToolContext(
        config=config,
        memory=Memory(config.memory_file),
        reminders=Reminders(config.reminders_file),
        confirm=lambda action: True,
        say=lambda text: None,
    )
    tools = {t.to_dict()["name"]: t for t in build_tools(ctx) if not isinstance(t, dict)}

    assert "хроника не ведётся" in tools["what_happened"].call({})
