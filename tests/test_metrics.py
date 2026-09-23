"""Расчёт метрик прогона.

Формулы перенесены из прототипа, где они считались через `numpy` и `scipy`. Здесь сторожатся
именно те свойства, ради которых перенос делался «как есть»: линейная интерполяция
перцентиля, наклон наименьших квадратов и то, что задержки считаются по всем запросам.
"""

from __future__ import annotations

import pytest

from partest_load.core.metrics import (
    RunMetrics,
    degradation_status,
    linear_slope,
    percentile,
)


# ── перцентиль ────────────────────────────────────────────────────────────────────────

def test_percentile_uses_nearest_rank_like_the_rest_of_the_family():
    """Ближайший ранг, а не интерполяция: одно определение на семейство.

    Здесь была интерполяция, как у `numpy.percentile`, а у владельца определения —
    ближайший ранг. Обе величины звались p95, и карта сравнивает p95 нагрузочного замера
    с p95 эталона из покрытия: сравнивались бы два разных числа.

    Прежняя редакция этого теста предсказывала переход дословно — «дал бы здесь 30 или 40,
    и все p95 в отчётах сдвинулись бы, не сломав ничего заметного». Сдвинулись; вот числа
    после сдвига, чтобы следующий читатель не «починил» их обратно.
    """
    values = [10, 20, 30, 40]

    assert percentile(values, 50) == pytest.approx(20.0)
    assert percentile(values, 95) == pytest.approx(40.0)
    assert percentile(values, 99) == pytest.approx(40.0)


def test_percentile_never_invents_a_value_that_was_not_measured():
    """Главное свойство ближайшего ранга, из-за которого он и выбран."""
    values = [10.0, 20.0, 30.0, 40.0, 1000.0]

    for q in (1.0, 25.0, 50.0, 75.0, 95.0, 99.0, 100.0):
        assert percentile(values, q) in values, (
            f"p{q} вернул значение, которого не было в замерах"
        )


def test_the_definition_matches_the_owner_of_the_contract():
    """Сторож против расхождения: определение живёт у владельца, здесь — копия.

    Расхождение в одном округлении вернуло бы ту же несравнимость, только тише. Тест
    пропускается, если пакет владельца не установлен: он объявлен зависимостью, но набор
    не должен падать из-за его отсутствия в чужом окружении.
    """
    payload = pytest.importorskip(
        "partest.reports.payload",
        reason="владелец определения не установлен в этом окружении",
    )

    samples = [
        [7.0],
        [10.0, 20.0],
        [1.0, 2.0, 3.0, 4.0, 5.0],
        [5.0, 5.0, 5.0, 100.0],
        [float(i) for i in range(1, 101)],
    ]
    for values in samples:
        for q in (50.0, 95.0, 99.0):
            assert percentile(values, q) == payload._percentile(values, q), (
                f"определения разошлись: n={len(values)}, q={q}"
            )


def test_percentile_edges():
    assert percentile([], 95) == 0.0
    assert percentile([7.5], 95) == 7.5
    assert percentile([1, 2, 3], 0) == 1.0
    assert percentile([1, 2, 3], 100) == 3.0


def test_percentile_does_not_require_sorted_input():
    assert percentile([40, 10, 30, 20], 50) == pytest.approx(20.0)


# ── наклон ────────────────────────────────────────────────────────────────────────────

def test_linear_slope_matches_least_squares():
    xs = [0.0, 1.0, 2.0, 3.0, 4.0]
    ys = [5.0, 7.0, 9.0, 11.0, 13.0]

    assert linear_slope(xs, ys) == pytest.approx(2.0)


def test_linear_slope_is_zero_when_undefined():
    assert linear_slope([1.0], [1.0]) == 0.0
    assert linear_slope([2.0, 2.0, 2.0], [1.0, 5.0, 9.0]) == 0.0
    assert linear_slope([1.0, 2.0], [1.0]) == 0.0


def test_degradation_status_thresholds_and_sign():
    """Пороги строгие, а знак не важен: резкое ускорение тоже подозрительно."""
    assert degradation_status(4.0) == "stable"
    assert degradation_status(4.1) == "minor"
    assert degradation_status(12.1) == "moderate"
    assert degradation_status(30.1) == "critical"
    assert degradation_status(-31.0) == "critical"


# ── агрегат ───────────────────────────────────────────────────────────────────────────

def _result(i: int, *, ok: bool = True, duration: float = 0.1,
            error_type: str | None = None, size_mb: float = 0.0) -> dict:
    return {
        "request_start_ts": 1_700_000_000.0 + i,
        "duration_sec": duration,
        "success": ok,
        "error_type": error_type,
        "size_mb": size_mb,
    }


def test_empty_run_gives_empty_metrics():
    metrics = RunMetrics.from_results([], wall_time=10.0)

    assert metrics.total_requests == 0
    assert metrics.real_rps == 0.0
    assert metrics.degradation_status == "stable"


def test_latency_counts_failed_requests_too():
    """Задержки считаются по всем запросам, включая таймауты.

    Если считать только успешные, система, начавшая отваливаться по таймауту, выглядит
    быстрее, чем была: самые медленные запросы просто исчезают из выборки.
    """
    results = ([_result(i, duration=0.1) for i in range(90)]
               + [_result(90 + i, ok=False, duration=5.0, error_type="timeout")
                  for i in range(10)])

    metrics = RunMetrics.from_results(results, wall_time=100.0)

    assert metrics.total_requests == 100
    assert metrics.p99_ms > 1000
    assert metrics.mean_ms == pytest.approx((90 * 100 + 10 * 5000) / 100)


def test_error_rate_and_error_types():
    results = ([_result(i) for i in range(8)]
               + [_result(8, ok=False, error_type="server_5xx"),
                  _result(9, ok=False, error_type=None)])

    metrics = RunMetrics.from_results(results, wall_time=10.0)

    assert metrics.success_requests == 8
    assert metrics.success_rate == pytest.approx(0.8)
    assert metrics.error_rate_pct == pytest.approx(20.0)
    # Ошибка без типа не теряется: в прототипе такой результат ронял подсчёт по KeyError.
    assert metrics.errors_by_type == {"server_5xx": 1, "unknown": 1}


def test_real_rps_is_requests_over_wall_time():
    metrics = RunMetrics.from_results([_result(i) for i in range(50)], wall_time=25.0)

    assert metrics.real_rps == pytest.approx(2.0)


def test_response_size_ignores_requests_without_a_body():
    results = [_result(0, size_mb=0.0), _result(1, size_mb=2.0), _result(2, size_mb=4.0)]

    metrics = RunMetrics.from_results(results, wall_time=3.0)

    assert metrics.avg_response_mb == pytest.approx(3.0)
    assert metrics.max_response_mb == pytest.approx(4.0)


def test_time_to_first_error_needs_a_meaningful_sample():
    """На выборке меньше двадцати запросов «первая ошибка» — шум, а не наблюдение."""
    few = [_result(0), _result(1, ok=False, error_type="timeout")]
    assert RunMetrics.from_results(few, wall_time=2.0).time_to_first_error_sec is None

    many = [_result(i) for i in range(30)]
    many[25] = _result(25, ok=False, error_type="timeout")
    metrics = RunMetrics.from_results(many, wall_time=30.0)
    assert metrics.time_to_first_error_sec == pytest.approx(25.0)


def test_no_error_means_no_time_to_first_error():
    results = [_result(i) for i in range(40)]

    assert RunMetrics.from_results(results, wall_time=40.0).time_to_first_error_sec is None


def _growing_run(count: int, first_ms: float, last_ms: float) -> list:
    step = (last_ms - first_ms) / (count - 1)
    return [_result(i * 2, duration=(first_ms + i * step) / 1000.0) for i in range(count)]


def test_degradation_is_noticed_when_latency_grows():
    metrics = RunMetrics.from_results(_growing_run(300, 100.0, 1000.0), wall_time=600.0)

    assert metrics.degradation_slope_pct_per_min > 0
    assert metrics.degradation_status in ("minor", "moderate", "critical")


def test_flat_run_is_called_stable():
    metrics = RunMetrics.from_results(_growing_run(300, 200.0, 200.0), wall_time=600.0)

    assert metrics.degradation_slope_pct_per_min == pytest.approx(0.0, abs=1e-6)
    assert metrics.degradation_status == "stable"


def test_degradation_is_not_guessed_from_a_short_run():
    """Меньше шестидесяти запросов — наклон не считается вовсе."""
    metrics = RunMetrics.from_results(_growing_run(50, 100.0, 5000.0), wall_time=100.0)

    assert metrics.degradation_slope_pct_per_min == 0.0
    assert metrics.degradation_status == "stable"


def test_degradation_needs_timestamps():
    results = [{"duration_sec": 0.1 * i, "success": True} for i in range(1, 101)]

    metrics = RunMetrics.from_results(results, wall_time=100.0)

    assert metrics.degradation_slope_pct_per_min == 0.0
