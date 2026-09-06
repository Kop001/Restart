"""Поведение на Windows.

Настоящая Windows для этих тестов не нужна: проверяется выбор ветки
по имени системы, а системные вызовы подменяются.
"""

from __future__ import annotations

import os
import platform

import pytest

from jarvis.config import Config
from jarvis.memory import Memory
from jarvis.reminders import Reminders
from jarvis.tools import build_tools
from jarvis.tools.context import ToolContext


@pytest.fixture
def tools(tmp_path, monkeypatch):
    monkeypatch.setenv("JARVIS_HOME", str(tmp_path / "state"))
    config = Config.load({"workspace": str(tmp_path), "confirm_mode": "auto"})
    ctx = ToolContext(
        config=config,
        memory=Memory(config.memory_file),
        reminders=Reminders(config.reminders_file),
        confirm=lambda action: True,
        say=lambda text: None,
    )
    return {t.to_dict()["name"]: t for t in build_tools(ctx) if not isinstance(t, dict)}


def test_open_url_uses_startfile_on_windows(tools, monkeypatch):
    """На Windows нет xdg-open — ссылку открывает os.startfile."""
    opened: list[str] = []
    monkeypatch.setattr(platform, "system", lambda: "Windows")
    monkeypatch.setattr(os, "startfile", opened.append, raising=False)

    result = tools["open_url"].call({"url": "https://example.com"})

    assert opened == ["https://example.com"]
    assert "открыл" in result


def test_open_url_reports_missing_startfile(tools, monkeypatch):
    """Если startfile недоступен, инструмент возвращает текст, а не падает."""
    monkeypatch.setattr(platform, "system", lambda: "Windows")
    monkeypatch.delattr(os, "startfile", raising=False)

    assert "startfile" in tools["open_url"].call({"url": "https://example.com"})


def test_open_url_uses_opener_on_unix(tools, monkeypatch):
    """На Unix по-прежнему xdg-open и его сородичи."""
    launched: list[list[str]] = []
    monkeypatch.setattr(platform, "system", lambda: "Linux")
    monkeypatch.setattr("jarvis.tools.system._opener", lambda: "xdg-open")
    monkeypatch.setattr(
        "jarvis.tools.system.subprocess.Popen",
        lambda cmd, **kwargs: launched.append(cmd),
    )

    tools["open_url"].call({"url": "https://example.com"})

    assert launched == [["xdg-open", "https://example.com"]]


def test_open_url_reports_missing_opener(tools, monkeypatch):
    monkeypatch.setattr(platform, "system", lambda: "Linux")
    monkeypatch.setattr("jarvis.tools.system._opener", lambda: None)

    assert "xdg-open" in tools["open_url"].call({"url": "https://example.com"})
