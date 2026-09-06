"""Интеграционный тест агента против заглушки Messages API.

Ключ API не нужен: SDK ходит на локальный сервер, который отдаёт заранее
заготовленные ответы потоком — тем же протоколом, что и настоящий API.
"""

from __future__ import annotations

import anthropic
import pytest
from stub_api import message, serve

from jarvis.agent import Agent
from jarvis.config import Config
from jarvis.memory import History

SCRIPT = [
    message([{"type": "tool_use", "id": "tu_1", "name": "current_time", "input": {}}], "tool_use"),
    message([{"type": "text", "text": "Половина одиннадцатого."}], "end_turn"),
]


@pytest.fixture
def stub_api():
    requests: list[dict] = []
    replies = iter(SCRIPT)
    server, base = serve(lambda req: next(replies), requests)
    yield base, requests
    server.shutdown()


def test_agent_runs_tool_loop(stub_api, tmp_path, monkeypatch):
    base_url, requests = stub_api
    monkeypatch.setenv("JARVIS_HOME", str(tmp_path / "state"))
    config = Config.load({"workspace": str(tmp_path), "confirm_mode": "auto"})
    client = anthropic.Anthropic(api_key="sk-ant-stub", base_url=base_url, max_retries=0)
    agent = Agent(config, client=client, confirm=lambda action: True, say=lambda text: None)

    answer = agent.ask("который час?")

    assert answer == "Половина одиннадцатого."
    assert len(requests) == 2, "должен быть второй запрос с результатом инструмента"

    first = requests[0]
    assert first["model"] == "claude-opus-5"
    assert first["thinking"] == {"type": "adaptive"}
    assert first["output_config"]["effort"] == config.effort
    assert first["fallbacks"] == "default"
    assert first["stream"] is True, "ответ должен идти потоком"
    # Персона идёт первым блоком и кэшируется — она не меняется между ходами.
    assert "cache_control" in first["system"][0]
    assert any(tool.get("name") == "web_search" for tool in first["tools"])

    second = requests[1]
    assert any(
        block.get("type") == "tool_result"
        for msg in second["messages"]
        if isinstance(msg["content"], list)
        for block in msg["content"]
    )

    assert agent.stats == {
        "requests": 2, "turns": 1, "input_tokens": 200,
        "output_tokens": 20, "cache_read_tokens": 0, "tool_calls": 1,
    }
    # История должна переживать перезапуск процесса — значит, быть простым JSON.
    assert len(History(config.history_file, 40).messages) == 4


def test_answer_arrives_in_pieces(stub_api, tmp_path, monkeypatch):
    """Джарвис должен отдавать текст по мере готовности, а не одним куском."""
    base_url, _ = stub_api
    monkeypatch.setenv("JARVIS_HOME", str(tmp_path / "state"))
    config = Config.load({"workspace": str(tmp_path), "confirm_mode": "auto"})
    client = anthropic.Anthropic(api_key="sk-ant-stub", base_url=base_url, max_retries=0)
    agent = Agent(config, client=client, confirm=lambda action: True, say=lambda text: None)

    pieces: list[str] = []
    agent.ask("который час?", on_text=pieces.append)

    assert "".join(pieces) == "Половина одиннадцатого."
