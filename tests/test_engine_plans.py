"""Арифметика расписания у каждого движка.

Кривая нагрузки — это то, ради чего движок существует, и единственное, что можно проверить
без стенда. Проверяется не форма списка, а свойства кривой: откуда начинается, чем
кончается, сколько длится в сумме и куда идёт монотонность.
"""

from __future__ import annotations

import pytest

from partest_load.configs.endpoints import EndpointConfig
from partest_load.configs.profiles import parse_profile
from partest_load.engines import BatchEngine, LinearEngine, RampEngine, StressEngine
from partest_load.engines.linear import FLOOR_CONCURRENCY


@pytest.fixture()
def endpoint() -> EndpointConfig:
    return EndpointConfig(name="demo", base_url="https://service.invalid", path="/v1/items",
                          method="POST", payloads=[{"body": {"q": "x"}}])


def plan_of(engine_cls, profile_data: dict, endpoint: EndpointConfig):
    return engine_cls(parse_profile(profile_data), endpoint).plan()


# ── linear ────────────────────────────────────────────────────────────────────────────

LINEAR = {"type": "linear", "duration_sec": 300, "target_concurrency": 200,
          "workers": 8, "ramp_up_sec": 45, "ramp_down_sec": 30}


def test_linear_ramp_up_starts_low_and_reaches_target(endpoint):
    phases = plan_of(LinearEngine, LINEAR, endpoint)
    ramp_up = [p for p in phases if p.name.startswith("ramp-up")]

    assert ramp_up[0].concurrency == FLOOR_CONCURRENCY
    assert ramp_up[-1].concurrency == LINEAR["target_concurrency"]
    assert [p.concurrency for p in ramp_up] == sorted(p.concurrency for p in ramp_up)
    # Разгон длится ровно столько, сколько заказано: фазы делят ramp_up_sec без остатка.
    assert sum(p.duration_sec for p in ramp_up) == pytest.approx(LINEAR["ramp_up_sec"])


def test_linear_has_one_steady_phase_of_declared_duration(endpoint):
    phases = plan_of(LinearEngine, LINEAR, endpoint)
    steady = [p for p in phases if p.name == "steady"]

    assert len(steady) == 1
    assert steady[0].concurrency == LINEAR["target_concurrency"]
    assert steady[0].duration_sec == LINEAR["duration_sec"]


def test_linear_ramp_down_ends_at_floor_and_never_stops_the_load(endpoint):
    phases = plan_of(LinearEngine, LINEAR, endpoint)
    ramp_down = [p for p in phases if p.name.startswith("ramp-down")]

    assert ramp_down[0].concurrency == LINEAR["target_concurrency"]
    assert ramp_down[-1].concurrency == FLOOR_CONCURRENCY
    assert all(p.concurrency >= FLOOR_CONCURRENCY for p in ramp_down)
    assert sum(p.duration_sec for p in ramp_down) == pytest.approx(LINEAR["ramp_down_sec"])


def test_linear_ramp_down_is_finer_grained_than_ramp_up():
    """Спад дробится мельче разгона — это решение, а не совпадение.

    Резкий сброс нагрузки даёт в отчёте всплеск задержек, который потом читают как
    деградацию. Уравняв число шагов, тест на «спад кончается на полу» остался бы зелёным.
    """
    profile = {**LINEAR, "ramp_up_sec": 60, "ramp_down_sec": 60}
    phases = LinearEngine(parse_profile(profile), None).plan()

    up = [p for p in phases if p.name.startswith("ramp-up")]
    down = [p for p in phases if p.name.startswith("ramp-down")]
    assert len(down) > len(up)


def test_linear_without_ramps_is_a_single_phase(endpoint):
    phases = plan_of(LinearEngine, {**LINEAR, "ramp_up_sec": 0, "ramp_down_sec": 0}, endpoint)
    assert [p.name for p in phases] == ["steady"]


# ── ramp ──────────────────────────────────────────────────────────────────────────────

RAMP = {
    "type": "ramp", "workers": 10,
    "phases": [
        {"name": "warm-up", "duration_sec": 60, "target_concurrency": 40},
        {"name": "grow", "duration_sec": 120, "start_concurrency": 40, "end_concurrency": 220},
        {"name": "steady", "duration_sec": 300, "target_concurrency": 220},
        {"name": "drop", "duration_sec": 90, "start_concurrency": 220, "end_concurrency": 10},
    ],
}


def test_ramp_fixed_phase_is_left_whole(endpoint):
    phases = plan_of(RampEngine, RAMP, endpoint)
    warm = [p for p in phases if p.name == "warm-up"]

    assert len(warm) == 1
    assert (warm[0].concurrency, warm[0].duration_sec) == (40, 60)


def test_ramp_growing_phase_goes_from_start_to_end(endpoint):
    phases = [p for p in plan_of(RampEngine, RAMP, endpoint) if p.name.startswith("grow")]
    levels = [p.concurrency for p in phases]

    assert levels[0] == 40
    assert levels[-1] == 220
    assert levels == sorted(levels)
    assert sum(p.duration_sec for p in phases) == pytest.approx(120)


def test_ramp_descending_phase_goes_down_to_its_end(endpoint):
    """Спуск обязан спускаться.

    Потолок подшага берётся как max(start, end) именно ради этого случая: ограничь его
    концом фазы, и спуск 220 → 10 превратился бы в ровную линию на десяти.
    """
    phases = [p for p in plan_of(RampEngine, RAMP, endpoint) if p.name.startswith("drop")]
    levels = [p.concurrency for p in phases]

    assert levels[0] == 220
    assert levels[-1] == 10
    assert levels == sorted(levels, reverse=True)


def test_ramp_keeps_phase_order_of_the_profile(endpoint):
    phases = plan_of(RampEngine, RAMP, endpoint)
    order = [n for n in ("warm-up", "grow", "steady", "drop")]
    positions = [min(i for i, p in enumerate(phases) if p.name.startswith(n)) for n in order]

    assert positions == sorted(positions)


def test_ramp_phase_without_name_gets_its_number(endpoint):
    profile = {"type": "ramp", "phases": [{"duration_sec": 30, "target_concurrency": 7}]}
    phases = plan_of(RampEngine, profile, endpoint)

    assert phases[0].name == "phase-1"


# ── stress ────────────────────────────────────────────────────────────────────────────

STRESS = {"type": "stress", "workers": 20, "initial_concurrency": 80, "max_concurrency": 600,
          "ramp_up_step": 40, "ramp_up_interval_sec": 15, "max_duration_sec": 1200}


def test_stress_ladder_climbs_by_step_and_respects_the_ceiling(endpoint):
    phases = plan_of(StressEngine, STRESS, endpoint)
    levels = [p.concurrency for p in phases]

    assert levels[0] == 80
    assert levels[1] - levels[0] == 40
    assert max(levels) <= 600
    assert all(p.duration_sec == 15 for p in phases)


def test_stress_ladder_is_cut_by_the_time_limit(endpoint):
    """Аварийный предел по времени режет лестницу, даже если потолок ещё не достигнут."""
    short = {**STRESS, "max_duration_sec": 60, "max_concurrency": 10_000}
    phases = plan_of(StressEngine, short, endpoint)

    assert len(phases) == 4  # 60 с / 15 с на уровень
    assert sum(p.duration_sec for p in phases) == pytest.approx(60)


# ── batch ─────────────────────────────────────────────────────────────────────────────

BATCH = {"type": "batch", "duration_sec": 600, "target_concurrency": 30,
         "workers": 1, "repeat_count": 3}


def test_batch_is_one_phase_with_the_whole_budget(endpoint):
    phases = plan_of(BatchEngine, BATCH, endpoint)

    assert len(phases) == 1
    assert (phases[0].concurrency, phases[0].duration_sec) == (30, 600)


def test_batch_does_not_mutate_the_endpoint_it_was_given(endpoint):
    """Движок работает на копии описания.

    Прототип дописывал `batch_mode` в чужую модель через `setattr`. Один эндпоинт,
    прогнанный batch-профилем, оставался «батчевым» для всех последующих профилей в том
    же процессе — и следующий прогон шёл не тем, чем его заказывали.
    """
    engine = BatchEngine(parse_profile(BATCH), endpoint)

    assert engine.endpoint.batch_mode is True
    assert engine.endpoint.repeat_count == 3
    assert endpoint.batch_mode is False
    assert endpoint.repeat_count == 1
