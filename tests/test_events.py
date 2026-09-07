"""Ядро событий: очередь, правила, порог, тишина, бюджет, срок жизни."""

from __future__ import annotations

import time
from datetime import datetime

import pytest

from jarvis.config import Config
from jarvis.events import SILENT, SPEAK, Event, EventLog
from jarvis.loop import EventWorker
from jarvis.rules import BY_MODEL, BY_RULE, ModelBudget, decide
from jarvis.triage import _parse


@pytest.fixture
def config(tmp_path, monkeypatch):
    monkeypatch.setenv("JARVIS_HOME", str(tmp_path / "state"))
    return Config.load({"workspace": str(tmp_path)})


@pytest.fixture
def anytime(config):
    """Тот же конфиг, но без часов тишины.

    Тесты исполнителя не про тишину, а прогон случается и в три ночи —
    тогда правило гасило события раньше модели, и тест падал на ровном месте.
    """
    config.quiet_from = config.quiet_to = 0
    return config


@pytest.fixture
def log(config):
    return EventLog(config.events_file, keep_days=config.chronicle_days)


# --- журнал ---

def test_event_starts_in_the_queue(log):
    event = log.emit("почта", "письмо", "письмо от коллеги", weight=70)

    assert event.pending
    assert log.pending() == [event]


def test_resolved_event_leaves_the_queue_but_stays_in_the_chronicle(log):
    event = log.emit("почта", "письмо", "рассылка", weight=10)

    log.resolve(event, SILENT, BY_RULE, note="ниже порога")

    assert log.pending() == []
    assert [e.id for e in log.recent()] == [event.id]
    assert event.note == "ниже порога"


def test_unknown_outcome_is_rejected(log):
    event = log.emit("почта", "письмо", "что-то")
    with pytest.raises(ValueError):
        log.resolve(event, "станцевать", BY_RULE)


def test_journal_survives_restart(config):
    first = EventLog(config.events_file, keep_days=30)
    first.emit("задача", "задача-готова", "отчёт готов", weight=70)

    second = EventLog(config.events_file, keep_days=30)

    assert [e.text for e in second.pending()] == ["отчёт готов"]


def test_private_mode_writes_no_journal(config):
    log = EventLog(config.events_file, keep_days=30, persist=False)
    log.emit("человек", "реплика", "секретный разговор")

    assert not config.events_file.exists()
    assert len(log.recent()) == 1


def test_failed_event_returns_to_the_queue_but_not_forever(log):
    event = log.emit("почта", "письмо", "проблемное")

    for _ in range(3):
        log.defer(event)

    assert log.pending() == [], "после трёх попыток событие перестаёт мешать"


def test_old_events_are_forgotten(config):
    log = EventLog(config.events_file, keep_days=30)
    old = log.emit("задача", "задача-готова", "древнее")
    log.resolve(old, SILENT, BY_RULE)
    old.at = time.time() - 40 * 86400
    log.emit("задача", "задача-готова", "свежее")
    log._save_locked()

    reopened = EventLog(config.events_file, keep_days=30)

    assert [e.text for e in reopened.recent()] == ["свежее"]


def test_chronicle_can_be_kept_forever(config):
    log = EventLog(config.events_file, keep_days=0)
    event = log.emit("задача", "задача-готова", "древнее")
    log.resolve(event, SILENT, BY_RULE)
    event.at = time.time() - 400 * 86400
    log._save_locked()

    reopened = EventLog(config.events_file, keep_days=0)

    assert len(reopened.recent()) == 1


# --- правила ---

DAY = datetime(2026, 9, 6, 14, 0)
NIGHT = datetime(2026, 9, 6, 2, 0)


@pytest.mark.parametrize(
    "event,when,outcome,why",
    [
        (Event("человек", "реплика", "привет"), NIGHT, SPEAK, "человек обратился"),
        (Event("время", "напоминание", "созвон"), NIGHT, SPEAK, "владелец сам этого ждал"),
        (Event("задача", "задача-готова", "готово"), NIGHT, SPEAK, "владелец сам этого ждал"),
        (Event("почта", "письмо", "рассылка", weight=20), DAY, SILENT, "ниже порога"),
        (Event("почта", "письмо", "коллега", weight=70), NIGHT, SILENT, "часы тишины"),
    ],
)
def test_rules_decide_without_the_model(event, when, outcome, why, config):
    verdict = decide(event, config, when)

    assert verdict is not None, "это событие модель видеть не должна"
    assert verdict.outcome == outcome
    assert why in verdict.why


def test_urgent_breaks_through_the_quiet_hours(config):
    event = Event("сборка", "упала", "всё сломалось", weight=95)
    assert decide(event, config, NIGHT) is None, "важное ночью решает модель, а не правило"


def test_ambiguous_event_goes_to_the_model(config):
    event = Event("почта", "письмо", "письмо от коллеги", weight=70)
    assert decide(event, config, DAY) is None


def test_noise_is_silenced_by_path(config):
    event = Event("файл", "файл", "изменился файл", weight=65,
                  details={"path": "/home/user/project/node_modules/x.js"})
    verdict = decide(event, config, DAY)

    assert verdict is not None
    assert verdict.outcome == SILENT


def test_quiet_hours_wrap_around_midnight(config):
    config.quiet_from, config.quiet_to = 23, 8
    event = Event("почта", "письмо", "письмо", weight=70)

    assert decide(event, config, datetime(2026, 9, 6, 23, 30)).outcome == SILENT
    assert decide(event, config, datetime(2026, 9, 6, 7, 0)).outcome == SILENT
    assert decide(event, config, datetime(2026, 9, 6, 9, 0)) is None


# --- бюджет модели ---

def test_budget_runs_out_and_recovers():
    budget = ModelBudget(per_hour=2)

    assert budget.allow()
    budget.spend()
    assert budget.allow()
    budget.spend()
    assert not budget.allow()

    budget._calls = [time.time() - 3700, time.time() - 3700]
    assert budget.allow(), "через час обращения снова доступны"


def test_zero_budget_forbids_everything():
    assert not ModelBudget(per_hour=0).allow()


# --- цикл ---

def test_worker_speaks_only_when_told_to(log, config):
    spoken: list[str] = []
    worker = EventWorker(log, config, speak=lambda e: spoken.append(e.text))

    log.emit("человек", "реплика", "привет")
    log.emit("почта", "письмо", "рассылка", weight=10)
    worker.drain()

    assert spoken == ["привет"]
    assert log.pending() == []


def test_worker_asks_the_model_only_for_the_unresolved(log, anytime):
    asked: list[Event] = []

    def ask(event):
        asked.append(event)
        return SPEAK, "похоже на важное"

    worker = EventWorker(log, anytime, speak=lambda e: None, ask_model=ask)
    log.emit("человек", "реплика", "привет")
    log.emit("почта", "письмо", "письмо от коллеги", weight=70)
    worker.drain()

    assert [e.text for e in asked] == ["письмо от коллеги"], "реплика модели не показывается"


def test_events_resolved_by_rule_never_reach_the_model(log, config):
    worker = EventWorker(log, config, speak=lambda e: None, ask_model=lambda e: (SPEAK, ""))
    log.emit("почта", "письмо", "рассылка", weight=10)
    worker.drain()

    assert log.recent()[0].sent_to_model is False
    assert log.recent()[0].decided_by == BY_RULE


def test_spent_budget_means_silence_not_a_call(log, anytime):
    anytime.model_calls_per_hour = 0
    worker = EventWorker(log, anytime, speak=lambda e: None, ask_model=lambda e: (SPEAK, ""))
    log.emit("почта", "письмо", "письмо от коллеги", weight=70)
    worker.drain()

    event = log.recent()[0]
    assert event.outcome == SILENT
    assert event.sent_to_model is False
    assert "бюджет" in event.note


def test_model_verdict_is_recorded_as_such(log, anytime):
    worker = EventWorker(log, anytime, speak=lambda e: None,
                         ask_model=lambda e: (SPEAK, "срочное письмо"))
    log.emit("почта", "письмо", "письмо от коллеги", weight=70)
    worker.drain()

    event = log.recent()[0]
    assert event.decided_by == BY_MODEL
    assert event.sent_to_model is True
    assert event.note == "срочное письмо"


def test_survey_counts_what_left_the_machine(log, anytime):
    worker = EventWorker(log, anytime, speak=lambda e: None, ask_model=lambda e: (SILENT, ""))
    log.emit("почта", "письмо", "рассылка", weight=10)
    log.emit("почта", "письмо", "письмо от коллеги", weight=70)
    worker.drain()

    assert log.survey() == {"всего": 2, "в очереди": 0, "уходило модели": 1}


# --- разбор ответа модели ---

@pytest.mark.parametrize(
    "answer,outcome",
    [
        ("сказать — письмо от начальника", SPEAK),
        ("промолчать, это рассылка", SILENT),
        ("Сказать. Важно.", SPEAK),
        ("ничего не понял", SILENT),
        ("", SILENT),
    ],
)
def test_model_answer_is_parsed_carefully(answer, outcome):
    assert _parse(answer)[0] == outcome


# --- не больше одного «сказать» за такт ---

def test_only_one_thing_is_said_per_tick(log, config):
    """Пять сообщений подряд — это не помощник, а сирена."""
    spoken: list[str] = []
    worker = EventWorker(log, config, speak=lambda e: spoken.append(e.text))
    for i in range(4):
        log.emit("время", "напоминание", f"напоминание {i}", weight=80)

    worker.drain()

    assert len(spoken) == 1
    assert len(log.pending()) == 3, "остальное ждёт следующего такта"


def test_the_rest_is_said_on_later_ticks(log, config):
    spoken: list[str] = []
    worker = EventWorker(log, config, speak=lambda e: spoken.append(e.text))
    for i in range(3):
        log.emit("время", "напоминание", f"напоминание {i}", weight=80)

    for _ in range(3):
        worker.drain()

    assert spoken == ["напоминание 0", "напоминание 1", "напоминание 2"]
    assert log.pending() == []


def test_silence_is_not_rationed(log, config):
    """Молчание голос не занимает — молчать можно сколько угодно за такт."""
    worker = EventWorker(log, config, speak=lambda e: None)
    for i in range(10):
        log.emit("почта", "письмо", f"рассылка {i}", weight=10)

    handled = worker.drain()

    assert handled == 10
    assert log.pending() == []


# --- подавление повторов после отказа ---

def test_dismissal_raises_the_bar_for_that_kind_only(config, tmp_path):
    from jarvis.rules import Attention

    attention = Attention(tmp_path / "attention.json")
    letter = Event("почта", "письмо", "письмо", weight=70)
    task = Event("задача", "задача-готова", "готово", weight=70)

    assert decide(letter, config, DAY, attention) is None, "сначала спорное — модели"

    attention.dismiss("почта", "письмо")
    attention.dismiss("почта", "письмо")

    verdict = decide(letter, config, DAY, attention)
    assert verdict is not None and verdict.outcome == SILENT
    assert "порога 90" in verdict.why, "порог поднялся на два шага"
    # Соседний род событий не пострадал.
    assert decide(task, config, DAY, attention).outcome == SPEAK


def test_reaction_lowers_the_bar_back(config, tmp_path):
    from jarvis.rules import Attention

    attention = Attention(tmp_path / "attention.json")
    attention.dismiss("почта", "письмо")
    attention.welcome("почта", "письмо")

    assert attention.raised_by(Event("почта", "письмо", "письмо")) == 0


def test_the_bar_does_not_rise_forever(tmp_path):
    from jarvis.rules import Attention

    attention = Attention(tmp_path / "attention.json")
    for _ in range(20):
        attention.dismiss("почта", "письмо")

    assert attention.raised_by(Event("почта", "письмо", "x")) == Attention.CEILING


def test_dismissals_survive_restart(tmp_path):
    from jarvis.rules import Attention

    path = tmp_path / "attention.json"
    Attention(path).dismiss("почта", "письмо")

    assert Attention(path).counts == {"почта/письмо": 1}


def test_private_mode_forgets_dismissals(tmp_path):
    from jarvis.rules import Attention

    path = tmp_path / "attention.json"
    Attention(path, persist=False).dismiss("почта", "письмо")

    assert not path.exists()
