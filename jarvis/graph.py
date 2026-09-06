"""Граф памяти: что с чем связано.

Панель показывает память списком, а список не отвечает на вопросы «о чём он
знает больше всего» и «что с чем пересекается». Связи здесь не придуманы для
красоты: тема берётся из поля факта, замена — из `superseded_by`, родство —
по общим основам слов, тем же стеммером, которым работает поиск. Что видно
на холсте, то и происходит внутри.
"""

from __future__ import annotations

from .morph import stems

# Две общих основы — уже не совпадение. Одной хватает только тогда, когда
# слово во всей памяти встретилось ровно у этой пары: значит, оно правда про них.
MIN_SHARED = 2
RARE = 2

# Родственных связей у факта — не больше трёх. Иначе плотный кусок памяти
# слипается в клубок, в котором уже ничего не разглядеть.
MAX_KIN = 3


def build(facts: list[dict]) -> dict:
    """Собирает узлы и рёбра для панели.

    Args:
        facts: все факты памяти, включая заменённые.

    Returns:
        Словарь с ключами `nodes` и `links`.
    """
    nodes: list[dict] = []
    links: list[dict] = []
    by_tag: dict[str, list[str]] = {}
    known: set[str] = set()

    for fact in facts:
        stale = bool(fact.get("superseded_by"))
        known.add(fact["id"])
        nodes.append({
            "id": fact["id"],
            "kind": "факт",
            "text": fact["text"],
            "tag": fact["tag"],
            "weight": fact.get("uses", 0),
            "importance": fact.get("importance", "normal"),
            "stale": stale,
            "last_used": fact.get("last_used"),
            "created_at": fact.get("created_at", ""),
            "source": fact.get("source", ""),
        })
        if not stale:
            by_tag.setdefault(fact["tag"], []).append(fact["id"])

    for tag, ids in by_tag.items():
        node_id = "тема:" + tag
        nodes.append({
            "id": node_id, "kind": "тема", "text": tag, "tag": tag,
            "weight": len(ids), "importance": "normal", "stale": False,
            "last_used": None, "created_at": "", "source": "",
        })
        links += [{"from": i, "to": node_id, "kind": "тема", "weight": 1} for i in ids]

    for fact in facts:
        heir = fact.get("superseded_by")
        # Наследник мог быть удалён насовсем — тогда ребро повисло бы в пустоту.
        if heir and heir in known:
            links.append({"from": fact["id"], "to": heir, "kind": "замена", "weight": 2})

    return {"nodes": nodes, "links": links + _kinship(facts)}


def _kinship(facts: list[dict]) -> list[dict]:
    """Связи по общим словам — между разными темами.

    Внутри одной темы факты и так стянуты к её узлу; повторять это ребром
    значит зашить очевидное поверх очевидного. Интересно другое: когда
    «работа» и «быт» держатся друг за друга словом «вторник».

    Одно общее слово обычно ничего не значит — «работа» найдётся почти везде.
    Но если во всей памяти оно встретилось только у этих двоих, то связывает
    их именно оно, а не частота.
    """
    live = [f for f in facts if not f.get("superseded_by")]
    words = {f["id"]: stems(f["text"] + " " + f["tag"], drop_stop=True) for f in live}

    spread: dict[str, int] = {}
    for bag in words.values():
        for word in bag:
            spread[word] = spread.get(word, 0) + 1

    best: dict[str, list[tuple[int, str]]] = {f["id"]: [] for f in live}
    for index, first in enumerate(live):
        for second in live[index + 1:]:
            if first["tag"] == second["tag"]:
                continue
            common = words[first["id"]] & words[second["id"]]
            rare = any(spread[word] <= RARE for word in common)
            if len(common) >= MIN_SHARED or (common and rare):
                shared = len(common)
                best[first["id"]].append((shared, second["id"]))
                best[second["id"]].append((shared, first["id"]))

    links: list[dict] = []
    seen: set[tuple[str, str]] = set()
    for fact_id, pairs in best.items():
        for shared, other in sorted(pairs, key=lambda pair: -pair[0])[:MAX_KIN]:
            pair = (fact_id, other) if fact_id < other else (other, fact_id)
            if pair in seen:
                continue
            seen.add(pair)
            links.append({"from": pair[0], "to": pair[1], "kind": "родство", "weight": shared})
    return links
