"""Поиск по смыслу слов: русские склонения не должны ломать память."""

from __future__ import annotations

import pytest

from jarvis.morph import matches, score, stem, stems


@pytest.mark.parametrize(
    "word,expected",
    [
        ("работа", "работ"), ("работы", "работ"), ("работе", "работ"),
        ("работаю", "работ"), ("работать", "работ"), ("работами", "работ"),
        ("место", "мест"), ("места", "мест"), ("местом", "мест"),
        ("четверг", "четверг"), ("четвергам", "четверг"),
        ("релиз", "релиз"), ("релизы", "релиз"),
    ],
)
def test_forms_of_one_word_share_a_stem(word, expected):
    assert stem(word) == expected


@pytest.mark.parametrize("word", ["дом", "код", "сон", "чай"])
def test_short_roots_are_left_alone(word):
    """«код» уже основа — обрезав, склеим его с чем попало."""
    assert stem(word) == word


@pytest.mark.parametrize("word", ["GitHub", "Python", "VK"])
def test_latin_is_only_lowercased(word):
    assert stem(word) == word.lower()


def test_yo_and_ye_are_the_same_letter():
    assert stem("приём") == stem("прием")


@pytest.mark.parametrize(
    "query,text",
    [
        ("где я работаю", "место работы владельца"),
        ("когда релиз", "релизы по четвергам"),
        ("что ты знаешь о моей работе", "работает в такой-то компании"),
        ("расскажи про аллергию", "аллергии на пенициллин"),
        ("врач", "приём у врача во вторник"),
    ],
)
def test_finds_across_word_forms(query, text):
    assert matches(query, text)


@pytest.mark.parametrize(
    "query,text",
    [
        ("кофе", "релизы по четвергам"),
        ("собака", "у владельца кошка"),
        ("код", "кодекс поведения"),
    ],
)
def test_does_not_find_what_is_not_there(query, text):
    assert not matches(query, text)


def test_empty_query_matches_anything():
    assert matches("", "что угодно")


def test_query_of_only_service_words_matches_anything():
    """«что как где» ничего не спрашивает — сузить по нему нельзя."""
    assert matches("что как где", "любой текст")


def test_more_matching_words_means_higher_weight():
    text = "релизы по четвергам в рабочем репозитории"
    assert score("релиз четверг", text) > score("релиз", text)


def test_service_words_are_dropped_before_stemming():
    """«когда» после обрезки станет «когд» и в списке служебных не найдётся."""
    assert "когда" not in stems("когда релиз", drop_stop=True)
    assert "когд" not in stems("когда релиз", drop_stop=True)
