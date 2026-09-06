"""Память: замена устаревшего, дубликаты, важность, старый формат."""

from __future__ import annotations

import json

import pytest

from jarvis.config import Config
from jarvis.memory import Memory
from jarvis.reminders import Reminders
from jarvis.tools import build_tools
from jarvis.tools.context import ToolContext


@pytest.fixture
def memory(tmp_path, monkeypatch):
    monkeypatch.setenv("JARVIS_HOME", str(tmp_path / "state"))
    config = Config.load({"workspace": str(tmp_path)})
    return Memory(config.memory_file)


@pytest.fixture
def tools(tmp_path, monkeypatch):
    monkeypatch.setenv("JARVIS_HOME", str(tmp_path / "state"))
    config = Config.load({"workspace": str(tmp_path), "confirm_mode": "auto"})
    memory = Memory(config.memory_file)
    ctx = ToolContext(
        config=config,
        memory=memory,
        reminders=Reminders(config.reminders_file),
        confirm=lambda action: True,
        say=lambda text: None,
    )
    named = {t.to_dict()["name"]: t for t in build_tools(ctx) if not isinstance(t, dict)}
    return named, memory


# --- замена устаревшего ---

def test_replacement_retires_the_old_fact(memory):
    old = memory.add("релизы по четвергам", "schedule")
    new = memory.add("релизы по вторникам", "schedule", replaces=old["id"])

    assert [f["id"] for f in memory.active()] == [new["id"]]
    assert [f["id"] for f in memory.stale()] == [old["id"]]
    # Старое не удалено — видно, чем именно вытеснено.
    assert memory.stale()[0]["superseded_by"] == new["id"]
    assert memory.stale()[0]["superseded_at"]


def test_retired_fact_leaves_the_prompt(memory):
    old = memory.add("работает в одном месте", "work")
    memory.add("работает в другом месте", "work", replaces=old["id"])

    prompt = memory.as_prompt()

    assert "другом" in prompt
    assert "одном" not in prompt, "заменённый факт не должен уезжать в модель"


def test_replacing_unknown_id_still_records_the_fact(memory):
    fact = memory.add("что-то новое", "general", replaces="нет-такого")

    assert fact in memory.active()
    assert memory.stale() == []


def test_replacement_chain(memory):
    first = memory.add("версия один", "tech")
    second = memory.add("версия два", "tech", replaces=first["id"])
    third = memory.add("версия три", "tech", replaces=second["id"])

    assert [f["id"] for f in memory.active()] == [third["id"]]
    assert len(memory.stale()) == 2


# --- дубликаты ---

def test_exact_duplicate_is_not_stored_twice(memory):
    first = memory.add("пьёт кофе без сахара", "personal")
    again = memory.add("пьёт кофе без сахара", "personal")

    assert again["id"] == first["id"]
    assert len(memory.search_all()) == 1


def test_duplicate_ignores_punctuation_and_case(memory):
    first = memory.add("Пьёт кофе без сахара!", "personal")
    again = memory.add("пьёт кофе, без сахара", "personal")

    assert again["id"] == first["id"]


def test_retired_fact_does_not_block_saying_it_again(memory):
    old = memory.add("релизы по четвергам", "schedule")
    memory.add("релизы по вторникам", "schedule", replaces=old["id"])

    # Вернулись к прежнему порядку — это новый действующий факт, а не дубликат.
    back = memory.add("релизы по четвергам", "schedule")

    assert back["id"] != old["id"]
    assert back in memory.active()


def test_empty_fact_is_rejected(memory):
    with pytest.raises(ValueError):
        memory.add("   ")


# --- важность и счётчик обращений ---

def test_importance_is_visible_in_prompt(memory):
    memory.add("аллергия на пенициллин", "health", importance="high")
    memory.add("пьёт кофе без сахара", "personal")

    prompt = memory.as_prompt()

    assert "(health!)" in prompt
    assert "(personal)" in prompt


def test_unknown_importance_falls_back_to_normal(memory):
    fact = memory.add("что-то", "general", importance="очень-очень")
    assert fact["importance"] == "normal"


def test_search_counts_uses_only_when_asked(memory):
    memory.add("живёт в таком-то городе", "personal")

    memory.search("город")
    assert memory.active()[0]["uses"] == 0

    memory.search("город", count=True)
    assert memory.active()[0]["uses"] == 1


def test_search_hides_retired_by_default(memory):
    old = memory.add("старое сведение", "general")
    memory.add("новое сведение", "general", replaces=old["id"])

    assert len(memory.search("сведение")) == 1
    assert len(memory.search("сведение", include_stale=True)) == 2


# --- старый формат ---

def test_reads_memory_written_by_previous_version(tmp_path, monkeypatch):
    """Файлы первой версии не должны ломать чтение."""
    monkeypatch.setenv("JARVIS_HOME", str(tmp_path / "state"))
    config = Config.load({"workspace": str(tmp_path)})
    config.memory_file.parent.mkdir(parents=True, exist_ok=True)
    config.memory_file.write_text(
        json.dumps([{"id": "old00001", "text": "давний факт", "tag": "work",
                     "created_at": "2026-01-01 10:00"}], ensure_ascii=False),
        encoding="utf-8",
    )

    memory = Memory(config.memory_file)

    assert len(memory.active()) == 1
    assert memory.active()[0]["uses"] == 0
    assert memory.active()[0]["superseded_by"] is None
    # И такой факт можно заменить наравне с новыми.
    memory.add("новый факт", "work", replaces="old00001")
    assert len(memory.stale()) == 1


# --- через инструменты модели ---

def test_tool_reports_replacement(tools):
    named, memory = tools
    old = memory.add("релизы по четвергам", "schedule")

    result = named["remember_fact"].call(
        {"text": "релизы по вторникам", "tag": "schedule", "replaces": old["id"]}
    )

    assert f"заменил {old['id']}" in result
    assert len(memory.active()) == 1


def test_tool_reports_known_fact_instead_of_duplicating(tools):
    named, memory = tools
    memory.add("пьёт кофе без сахара", "personal")

    result = named["remember_fact"].call({"text": "пьёт кофе без сахара", "tag": "personal"})

    assert "уже помню" in result
    assert len(memory.search_all()) == 1


def test_recall_marks_important_and_retired(tools):
    named, memory = tools
    memory.add("аллергия на пенициллин", "health", importance="high")
    old = memory.add("старое", "general")
    memory.add("новое", "general", replaces=old["id"])

    assert "!важно" in named["recall_facts"].call({"query": "аллергия"})
    assert "заменён на" in named["recall_facts"].call({"query": "старое", "include_stale": True})
