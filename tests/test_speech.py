"""Нарезка ответа на фразы: говорить на ходу, но законченными мыслями."""

from __future__ import annotations

import pytest

from jarvis.speech import Phrases


@pytest.fixture
def said():
    return []


def feed_all(said, pieces, min_length=12):
    phrases = Phrases(said.append, min_length=min_length)
    for piece in pieces:
        phrases.feed(piece)
    phrases.flush()
    return said


def test_complete_phrase_is_spoken_before_the_rest_arrives(said):
    phrases = Phrases(said.append)
    phrases.feed("Свободно 214 гигабайт. ")

    assert said == ["Свободно 214 гигабайт."], "первую фразу не надо ждать"


def test_unfinished_phrase_waits(said):
    phrases = Phrases(said.append)
    phrases.feed("Свободно 214 гигабайт, но ")

    assert said == []


def test_stream_is_cut_by_sentences(said):
    feed_all(said, ["Свободно 214 ", "гигабайт. Больше всего ", "занимает кэш. Почистить?"])

    assert said == ["Свободно 214 гигабайт.", "Больше всего занимает кэш.", "Почистить?"]


def test_tail_without_punctuation_is_still_spoken(said):
    feed_all(said, ["Готово, файл записан"])

    assert said == ["Готово, файл записан"]


def test_short_answer_is_not_chopped_into_gasps(said):
    feed_all(said, ["Да. Сделано, файл записан в рабочий каталог."])

    assert said == ["Да. Сделано, файл записан в рабочий каталог."]


def test_abbreviation_does_not_end_a_phrase(said):
    feed_all(said, ["Файлы, т.е. документы и картинки, лежат в другой папке."])

    assert said == ["Файлы, т.е. документы и картинки, лежат в другой папке."]


def test_line_break_ends_a_phrase(said):
    feed_all(said, ["Первая строка ответа\nвторая строка ответа"])

    assert said == ["Первая строка ответа", "вторая строка ответа"]


def test_nothing_is_said_about_nothing(said):
    feed_all(said, ["", "   ", ""])

    assert said == []


def test_question_and_exclamation_end_phrases(said):
    feed_all(said, ["Всё получилось! Показать отчёт целиком?"])

    assert said == ["Всё получилось!", "Показать отчёт целиком?"]
