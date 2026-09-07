"""Память разговоров: запись, поиск по словам, срок жизни, промпт."""

from __future__ import annotations

import time

import pytest

from jarvis.recall import Recall, when


@pytest.fixture
def recall(tmp_path):
    return Recall(tmp_path / "recall.json")


def test_обмен_репликами_ложится_на_диск(tmp_path):
    talks = Recall(tmp_path / "recall.json")
    talks.add("где мы держим конспекты", "в work/, не в заметках телефона")

    # Новый объект читает тот же файл: разговор переживает перезапуск.
    again = Recall(tmp_path / "recall.json")
    assert len(again) == 1
    assert again.recent()[0]["asked"] == "где мы держим конспекты"


def test_пустое_не_пишется(recall):
    recall.add("", "ответ")
    recall.add("вопрос", "   ")
    assert len(recall) == 0


def test_длинную_реплику_режем(recall):
    episode = recall.add("а" * 5000, "б" * 5000)
    assert len(episode["asked"]) < 700
    assert episode["answered"].endswith("…")


def test_поиск_идёт_по_основам_слов(recall):
    recall.add("когда у нас релизы", "по вторникам утром")
    recall.add("что с диском", "свободно 214 ГБ")

    found = recall.search("расскажи про релиз")

    assert len(found) == 1
    assert found[0]["answered"] == "по вторникам утром"


def test_одно_слово_поднимает_разговор_только_по_прямой_просьбе(recall):
    recall.add("привет", "здравствуйте")

    # Сам в промпт по одному слову не лезет — притащил бы что попало.
    assert recall.as_prompt("привет") == ""
    # А спросили нарочно — нашёл: на то и спрашивали.
    assert len(recall.search("привет")) == 1


def test_при_равном_счёте_новое_впереди_старого(recall):
    старое = recall.add("что с релизами", "по четвергам")
    time.sleep(0.01)
    новое = recall.add("что с релизами", "по вторникам")

    found = recall.search("что с релизами")
    assert [e["id"] for e in found[:2]] == [новое["id"], старое["id"]]


def test_старое_забывается_по_сроку(tmp_path):
    talks = Recall(tmp_path / "recall.json", keep_days=30)
    talks.add("давний вопрос", "давний ответ")
    talks.episodes[0]["at"] = time.time() - 31 * 86400

    talks.add("свежий вопрос", "свежий ответ")

    assert [e["asked"] for e in talks.episodes] == ["свежий вопрос"]


def test_нулевой_срок_значит_навсегда(tmp_path):
    talks = Recall(tmp_path / "recall.json", keep_days=0)
    talks.add("давний вопрос", "давний ответ")
    talks.episodes[0]["at"] = time.time() - 3650 * 86400

    talks.add("свежий вопрос", "свежий ответ")

    assert len(talks) == 2


def test_приватный_режим_не_пишет_на_диск(tmp_path):
    path = tmp_path / "recall.json"
    talks = Recall(path, persist=False)
    talks.add("вопрос", "ответ")

    assert len(talks) == 1, "внутри сессии разговор помнится"
    assert not path.exists(), "но на диск не ложится"


def test_блок_промпта_пуст_если_ничего_не_совпало(recall):
    recall.add("что с диском", "свободно 214 ГБ")
    assert recall.as_prompt("свари мне кофе") == ""


def test_блок_промпта_упоминает_давность(recall):
    recall.add("когда у нас релизы", "по вторникам утром")

    block = recall.as_prompt("что там с релизами по вторникам")

    assert "по вторникам" in block
    assert "сегодня" in block


def test_давность_называется_по_человечески():
    now = time.time()
    assert when(now) == "сегодня"
    assert when(now - 86400 * 1.5) == "вчера"
    assert when(now - 86400 * 3) == "3 дн. назад"
    assert when(now - 86400 * 10) == "1 нед. назад"
    assert when(now - 86400 * 400).count("-") == 2


def test_агенту_отдают_ту_же_память_а_не_новую(tmp_path, monkeypatch):
    """Пустая память разговоров ложна по длине — и `или` заводил вторую.

    Две копии на один файл дерутся за промежуточный `.tmp`: одна его
    подменяет, вторая не находит и падает. Ловилось только на трёх
    одновременных фоновых задачах.
    """
    import anthropic

    from jarvis.agent import Agent
    from jarvis.config import Config

    monkeypatch.setenv("JARVIS_HOME", str(tmp_path / "state"))
    config = Config.load({"workspace": str(tmp_path)})
    shared = Recall(config.recall_file)

    agent = Agent(config, client=anthropic.Anthropic(api_key="sk-ant-stub"), recall=shared)

    assert agent.recall is shared
    assert agent.ctx.recall is shared
