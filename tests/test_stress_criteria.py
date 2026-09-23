"""Критерий обрыва и точка насыщения у stress-движка.

Это единственный движок, чьё расписание обрывается по наблюдению, а не по часам. Решение
«дальше не растим» и ответ «сломалось на таком-то уровне» — два разных вычисления, и оба
считаются без сети.
"""

from __future__ import annotations

from partest_load.core.metrics import RunMetrics
from partest_load.engines.stress import error_rate_pct, saturation_point, stop_reasons


def _metrics(error_rate: float = 0.0, p99: float = 100.0, server_5xx: int = 0) -> RunMetrics:
    return RunMetrics(error_rate_pct=error_rate, p99_ms=p99,
                      errors_by_type={"server_5xx": server_5xx} if server_5xx else {})


def test_healthy_window_gives_no_reason_to_stop():
    assert stop_reasons(_metrics(), sample_size=300, max_error_rate=8.0,
                        max_p99_ms=18000, max_5xx_rate=2.0) == []


def test_error_rate_at_the_threshold_already_stops():
    """Порог включительный. Строгое «больше» отодвинуло бы обрыв на целый уровень выше."""
    reasons = stop_reasons(_metrics(error_rate=8.0), sample_size=300, max_error_rate=8.0,
                           max_p99_ms=18000, max_5xx_rate=2.0)

    assert len(reasons) == 1
    assert "8.0%" in reasons[0]


def test_slow_p99_stops_even_without_errors():
    reasons = stop_reasons(_metrics(p99=18000), sample_size=300, max_error_rate=8.0,
                           max_p99_ms=18000, max_5xx_rate=2.0)

    assert any("p99" in r for r in reasons)


def test_five_hundreds_are_counted_as_a_share_of_the_window():
    """Доля 5xx считается от размера окна, а не от числа ошибок."""
    six_of_three_hundred = stop_reasons(_metrics(error_rate=2.0, server_5xx=6),
                                        sample_size=300, max_error_rate=8.0,
                                        max_p99_ms=18000, max_5xx_rate=2.0)
    assert any("5xx" in r for r in six_of_three_hundred)

    five_of_three_hundred = stop_reasons(_metrics(error_rate=1.7, server_5xx=5),
                                         sample_size=300, max_error_rate=8.0,
                                         max_p99_ms=18000, max_5xx_rate=2.0)
    assert five_of_three_hundred == []


def test_empty_window_never_stops_the_run():
    assert stop_reasons(_metrics(error_rate=100.0, p99=99999), sample_size=0,
                        max_error_rate=8.0, max_p99_ms=18000, max_5xx_rate=2.0) == []


def test_error_rate_of_a_level():
    level = [{"success": True}] * 9 + [{"success": False}]

    assert error_rate_pct(level) == 10.0
    assert error_rate_pct([]) == 0.0


def _level(concurrency: int, total: int, failed: int):
    results = [{"success": i >= failed} for i in range(total)]
    return concurrency, results


def test_saturation_point_is_the_first_level_that_broke():
    levels = [_level(80, 200, 0), _level(120, 200, 4), _level(160, 200, 30),
              _level(200, 200, 120)]

    assert saturation_point(levels, max_error_rate=8.0) == 160


def test_saturation_point_is_an_integer_or_none():
    """В прототипе сюда писалась строка `"approx"` — в поле, объявленное целым числом.

    До отчёта это значение не доезжало: модель прогона его не принимала, то есть главный
    результат stress-прогона терялся целиком.
    """
    broken = saturation_point([_level(90, 200, 100)], max_error_rate=8.0)
    assert isinstance(broken, int)

    assert saturation_point([_level(90, 200, 0)], max_error_rate=8.0) is None
    assert saturation_point([], max_error_rate=8.0) is None


def test_thin_level_is_not_called_a_saturation_point():
    """Уровень из пяти запросов, где один упал, — это не предел системы, а шум."""
    levels = [_level(100, 5, 1), _level(140, 200, 0)]

    assert saturation_point(levels, max_error_rate=8.0) is None
