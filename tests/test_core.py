"""Тесты, которым не нужен ни ключ API, ни микрофон."""

from __future__ import annotations

import json
from datetime import datetime

import pytest

from jarvis.config import Config
from jarvis.memory import History, Memory
from jarvis.reminders import Reminders, parse_when
from jarvis.session import strip_wake_word
from jarvis.tools import build_tools
from jarvis.tools.context import ToolContext, ToolError


@pytest.fixture
def config(tmp_path, monkeypatch):
    monkeypatch.setenv("JARVIS_HOME", str(tmp_path / "state"))
    return Config.load({"workspace": str(tmp_path / "work"), "confirm_mode": "auto"})


@pytest.fixture
def ctx(config, tmp_path):
    (tmp_path / "work").mkdir(exist_ok=True)
    said: list[str] = []
    return ToolContext(
        config=config,
        memory=Memory(config.memory_file),
        reminders=Reminders(config.reminders_file),
        confirm=lambda action: True,
        say=said.append,
    )


def test_memory_roundtrip(config):
    memory = Memory(config.memory_file)
    fact = memory.add("владелец пьёт кофе без сахара", "personal")
    assert memory.search("кофе")
    assert memory.search(tag="personal")
    assert "кофе" in memory.as_prompt()

    reloaded = Memory(config.memory_file)
    assert [f["text"] for f in reloaded.facts] == [fact["text"]]
    assert reloaded.forget(fact["id"])
    assert not reloaded.forget(fact["id"])


def test_history_trims_on_clean_user_turn(config):
    history = History(config.history_file, max_turns=2)
    history.extend([
        {"role": "user", "content": "первый"},
        {"role": "assistant", "content": [{"type": "tool_use", "id": "t1"}]},
        {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "t1"}]},
        {"role": "assistant", "content": "готово"},
        {"role": "user", "content": "второй"},
    ])
    # Срез не должен начинаться с tool_result — иначе API отвергнет запрос.
    assert history.messages[0] == {"role": "user", "content": "второй"}


def test_parse_when_variants():
    now = datetime(2026, 9, 6, 10, 0)
    assert parse_when("через 10 минут", now) == datetime(2026, 9, 6, 10, 10)
    assert parse_when("в 18:30", now) == datetime(2026, 9, 6, 18, 30)
    assert parse_when("в 09:00", now) == datetime(2026, 9, 7, 9, 0)  # время уже прошло — завтра
    assert parse_when("2026-09-07 09:00", now) == datetime(2026, 9, 7, 9, 0)
    with pytest.raises(ValueError):
        parse_when("когда-нибудь", now)


def test_reminders_fire_once(config):
    reminders = Reminders(config.reminders_file)
    item = reminders.add("выпить воды", "через 1 сек")
    assert reminders.pop_due(datetime.now()) == []

    due = datetime.strptime(item["due"], "%Y-%m-%d %H:%M:%S")
    fired = reminders.pop_due(due)
    assert [i["id"] for i in fired] == [item["id"]]
    # Повторно то же напоминание не срабатывает.
    assert reminders.pop_due(due) == []
    assert reminders.pending() == []


@pytest.mark.parametrize(
    "phrase,expected",
    [
        ("Джарвис, включи музыку", "включи музыку"),
        ("джарвиз какая погода", "какая погода"),
        ("Привет, как дела", None),
        ("Джарвис", ""),
    ],
)
def test_strip_wake_word(phrase, expected):
    assert strip_wake_word(phrase, "джарвис") == expected


def test_path_sandbox(ctx, tmp_path):
    with pytest.raises(ToolError):
        ctx.resolve_path("/etc/passwd")
    assert ctx.resolve_path("notes.txt") == (tmp_path / "work" / "notes.txt")


def test_confirm_policy(ctx):
    ctx.config.confirm_mode = "deny"
    with pytest.raises(ToolError):
        ctx.ensure_allowed("удалить всё")
    ctx.config.confirm_mode = "auto"
    ctx.ensure_allowed("удалить всё")  # не должно бросать


def test_tools_have_valid_schemas(ctx):
    tools = build_tools(ctx)
    names = []
    for tool in tools:
        schema = tool if isinstance(tool, dict) else tool.to_dict()
        names.append(schema["name"])
        json.dumps(schema)  # схема должна быть сериализуемой
    assert {"run_shell", "read_file", "remember_fact", "add_reminder", "web_search"} <= set(names)
    assert len(names) == len(set(names)), "имена инструментов должны быть уникальны"


def test_file_tools_write_and_read(ctx):
    tools = {t.to_dict()["name"]: t for t in build_tools(ctx) if not isinstance(t, dict)}
    tools["write_file"].call({"path": "note.txt", "content": "привет"})
    assert "привет" in tools["read_file"].call({"path": "note.txt"})
    assert "note.txt" in tools["list_dir"].call({})
