"""Граф памяти: узлы, темы, замены и родство по словам."""

from __future__ import annotations

import pytest

from jarvis.graph import build
from jarvis.memory import Memory


@pytest.fixture
def memory(tmp_path):
    return Memory(tmp_path / "memory.json")


def kinds(graph, kind):
    return [link for link in graph["links"] if link["kind"] == kind]


def node(graph, node_id):
    return next(n for n in graph["nodes"] if n["id"] == node_id)


def test_тема_становится_узлом_и_собирает_свои_факты(memory):
    memory.add("работаю в лаборатории", tag="работа")
    memory.add("выхожу из дома в восемь", tag="работа")
    memory.add("не ем сахар", tag="здоровье")

    graph = build(memory.search_all())
    themes = {n["text"]: n for n in graph["nodes"] if n["kind"] == "тема"}

    assert themes["работа"]["weight"] == 2
    assert themes["здоровье"]["weight"] == 1
    assert len(kinds(graph, "тема")) == 3


def test_заменённый_факт_не_держит_тему_но_остаётся_на_карте(memory):
    old = memory.add("работаю в лаборатории", tag="работа")
    memory.add("работаю в институте", tag="работа", replaces=old["id"])

    graph = build(memory.search_all())

    # Тема считает только действующее, иначе карта врёт о размере.
    assert node(graph, "тема:работа")["weight"] == 1
    assert node(graph, old["id"])["stale"] is True
    assert len(kinds(graph, "замена")) == 1


def test_ребро_замены_не_повисает_после_удаления_наследника(memory):
    old = memory.add("работаю в лаборатории", tag="работа")
    new = memory.add("работаю в институте", tag="работа", replaces=old["id"])
    memory.forget(new["id"])

    assert kinds(build(memory.search_all()), "замена") == []


def test_родство_ищется_по_основам_а_не_по_буквам(memory):
    memory.add("работаю с утренними сменами", tag="работа")
    memory.add("после утренней смены болит спина", tag="здоровье")

    links = kinds(build(memory.search_all()), "родство")

    # «утренними» и «утренней», «сменами» и «смены» — одно и то же слово.
    assert len(links) == 1
    assert links[0]["weight"] >= 2


def test_частое_слово_родством_не_считается(memory):
    memory.add("люблю кофе по утрам", tag="привычки")
    memory.add("на работе кофе плохой", tag="работа")
    memory.add("кофе закончился", tag="быт")
    memory.add("кофе перед сном мешает", tag="здоровье")

    # «Кофе» тут у всех — связывать по нему всех со всеми бессмысленно.
    assert kinds(build(memory.search_all()), "родство") == []


def test_редкое_слово_связывает_и_в_одиночку(memory):
    memory.add("релизы выкатываем по вторникам", tag="работа")
    memory.add("мусор выносят по вторникам вечером", tag="быт")

    links = kinds(build(memory.search_all()), "родство")

    # «Вторник» больше нигде не встречается — значит, он и правда про эти два.
    assert len(links) == 1
    assert links[0]["weight"] == 1


def test_внутри_темы_родства_не_рисуем(memory):
    memory.add("утренняя смена начинается рано", tag="работа")
    memory.add("утренняя смена длиннее вечерней", tag="работа")

    # Оба и так висят на узле темы; второе ребро ничего не добавит.
    assert kinds(build(memory.search_all()), "родство") == []


def test_у_факта_не_больше_трёх_родственников(memory):
    memory.add("утренняя смена начинается рано", tag="работа")
    for number, tag in enumerate(("здоровье", "привычки", "быт", "спорт", "учёба")):
        memory.add(f"утренняя смена мешает {number}", tag=tag)

    links = kinds(build(memory.search_all()), "родство")
    counted: dict[str, int] = {}
    for link in links:
        counted[link["from"]] = counted.get(link["from"], 0) + 1
        counted[link["to"]] = counted.get(link["to"], 0) + 1

    # Ограничение одностороннее: узел набирает свои три, но соседи могут
    # выбрать его же. Важно, что клубок не смыкается полностью.
    assert links
    assert max(counted.values()) < len(memory.active())


def test_обращение_к_памяти_видно_на_карте(memory):
    memory.add("работаю в лаборатории", tag="работа")
    memory.search("где я работаю", count=True)

    found = next(n for n in build(memory.search_all())["nodes"] if n["kind"] == "факт")
    assert found["weight"] == 1
    assert found["last_used"]


def test_пустая_память_даёт_пустой_граф(memory):
    assert build(memory.search_all()) == {"nodes": [], "links": []}
