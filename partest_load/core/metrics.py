"""Метрики прогона: задержки, ошибки, насыщение, наклон деградации.

Считается на чистом Python. В прототипе перцентили брались из `numpy`, а наклон
деградации — из `scipy.stats.linregress`; scipy при этом не был объявлен ни в одном
манифесте, и модуль не импортировался вовсе. Метрики — ядро пакета, а numpy и pandas
вынесены в extra `report` намеренно: тот, кто снимает только артефакт, не должен платить
за отчёт. Поэтому обе формулы перенесены как есть, а не заменены другими:
`percentile` повторяет линейную интерполяцию `numpy.percentile`, `linear_slope` —
наклон из `linregress`.
"""

from __future__ import annotations

import math
from collections import Counter
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Sequence

# Пороги наклона p95 (% в минуту), по которым прогон получает словесную оценку.
DEGRADATION_MINOR = 4.0
DEGRADATION_MODERATE = 12.0
DEGRADATION_CRITICAL = 30.0


def percentile(values: Sequence[float], q: float) -> float:
    """Перцентиль ближайшего ранга — одно определение на всё семейство.

    Здесь была линейная интерполяция, как у `numpy.percentile`, а в `partest` — ближайший
    ранг, и обе величины назывались p95. Карта при этом сравнивает p95 нагрузочного замера
    с p95 эталона из покрытия: сравнивались бы два разных числа, и расхождение было бы не
    в пределах шума.

    Выбран ближайший ранг, а не интерполяция, хотя нагрузочные выборки большие и
    интерполяция на них точнее. Причина в том, что интерполяция **выдумывает** значение
    между двумя наблюдёнными, а у владельца определения выборки бывают из пяти замеров —
    там выдуманное число хуже менее точного. Сравнимость двух концов важнее доли процента
    на одном из них.

    Формула повторяет `partest.reports.payload._percentile` буквально: расхождение в
    округлении вернуло бы ту же несравнимость, только тише.
    """
    if not values:
        return 0.0
    ordered = sorted(values)
    index = max(0, min(len(ordered) - 1, int(round(q / 100.0 * len(ordered) + 0.5)) - 1))
    return float(ordered[index])


def linear_slope(xs: Sequence[float], ys: Sequence[float]) -> float:
    """Наклон прямой наименьших квадратов. Ноль, если наклон не определён."""
    n = len(xs)
    if n < 2 or n != len(ys):
        return 0.0
    mean_x = sum(xs) / n
    mean_y = sum(ys) / n
    variance = sum((x - mean_x) ** 2 for x in xs)
    if variance == 0:
        return 0.0
    covariance = sum((x - mean_x) * (y - mean_y) for x, y in zip(xs, ys))
    return covariance / variance


def degradation_status(slope_pct_per_min: float) -> str:
    """Словесная оценка наклона. Знак не важен: скачок вниз тоже подозрителен."""
    slope = abs(slope_pct_per_min)
    if slope > DEGRADATION_CRITICAL:
        return "critical"
    if slope > DEGRADATION_MODERATE:
        return "moderate"
    if slope > DEGRADATION_MINOR:
        return "minor"
    return "stable"


@dataclass
class RunMetrics:
    total_requests: int = 0
    success_requests: int = 0
    success_rate: float = 0.0
    real_rps: float = 0.0
    duration_sec: float = 0.0

    # Задержки считаются по ВСЕМ запросам — успешным, ошибочным и таймаутам. Иначе
    # система, которая начала отваливаться по таймауту, выглядит быстрее, чем была.
    p50_ms: float = 0.0
    p95_ms: float = 0.0
    p99_ms: float = 0.0
    max_ms: float = 0.0
    mean_ms: float = 0.0
    median_ms: float = 0.0

    error_rate_pct: float = 0.0
    errors_by_type: Dict[str, int] = field(default_factory=dict)

    avg_response_mb: float = 0.0
    max_response_mb: float = 0.0

    # Заполняется stress-движком после прогона.
    saturation_point_concurrency: Optional[int] = None
    time_to_first_error_sec: Optional[float] = None
    degradation_slope_pct_per_min: float = 0.0
    degradation_status: str = "stable"

    @classmethod
    def from_results(cls, results: List[dict], wall_time: float) -> "RunMetrics":
        if not results:
            return cls()

        durations_ms = [r.get("duration_sec", 0) * 1000 for r in results]
        ordered = sorted(durations_ms)
        total = len(results)
        success_cnt = sum(1 for r in results if r.get("success", False))
        sizes = [r.get("size_mb", 0) for r in results if r.get("size_mb", 0) > 0]

        m = cls(
            total_requests=total,
            success_requests=success_cnt,
            success_rate=success_cnt / total if total > 0 else 0.0,
            real_rps=total / wall_time if wall_time > 0 else 0.0,
            duration_sec=wall_time,
            p50_ms=percentile(ordered, 50),
            p95_ms=percentile(ordered, 95),
            p99_ms=percentile(ordered, 99),
            max_ms=max(ordered) if ordered else 0.0,
            mean_ms=sum(ordered) / total,
            median_ms=percentile(ordered, 50),
            error_rate_pct=(total - success_cnt) / total * 100 if total > 0 else 0.0,
            errors_by_type=dict(
                Counter(
                    r.get("error_type") or "unknown"
                    for r in results
                    if not r.get("success", False)
                )
            ),
            avg_response_mb=sum(sizes) / len(sizes) if sizes else 0.0,
            max_response_mb=max(sizes) if sizes else 0.0,
        )

        m.time_to_first_error_sec = _time_to_first_error(results, m.error_rate_pct)
        slope = _degradation_slope(results, durations_ms)
        if slope is not None:
            m.degradation_slope_pct_per_min = slope
            m.degradation_status = degradation_status(slope)
        return m


def _time_to_first_error(results: List[dict], error_rate_pct: float) -> Optional[float]:
    """Секунды от первого запроса до первой ошибки.

    Меньше двадцати запросов — не считаем: на такой выборке «первая ошибка» это шум.
    """
    if len(results) <= 20 or error_rate_pct <= 0:
        return None
    first_error = next((r for r in results if not r.get("success")), None)
    if not first_error:
        return None
    if "request_start_ts" not in results[0] or "request_start_ts" not in first_error:
        return None
    return first_error["request_start_ts"] - results[0]["request_start_ts"]


def _degradation_slope(results: List[dict], durations_ms: List[float]) -> Optional[float]:
    """Рост p95 в процентах за минуту, по регрессии p95 в скользящих окнах."""
    if len(results) < 60 or "request_start_ts" not in results[0]:
        return None

    times = [r.get("request_start_ts", 0.0) for r in results]
    window = max(40, len(results) // 15)
    step = max(1, window // 3)

    p95_values: List[float] = []
    mid_times: List[float] = []
    for i in range(window, len(durations_ms), step):
        chunk = durations_ms[i - window:i]
        if len(chunk) < window // 2:
            continue
        p95_values.append(percentile(chunk, 95))
        time_chunk = times[i - window:i]
        mid_times.append(sum(time_chunk) / len(time_chunk))

    if len(p95_values) < 4:
        return None
    avg_p95 = sum(p95_values) / len(p95_values)
    if avg_p95 <= 1:
        return None
    return (linear_slope(mid_times, p95_values) * 60 / avg_p95) * 100
