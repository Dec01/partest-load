"""Сборка прогона: профиль плюс эндпоинт → результаты, метрики, события.

Раньше раннер получал строковый ключ и сам ходил за ним в `configs.endpoints` —
модуль с эндпоинтами заказчика. Теперь эндпоинт приходит объектом: библиотека не знает
и не должна знать, откуда потребитель его взял.
"""

from __future__ import annotations

import time
from typing import List, Tuple

from ..configs.endpoints import EndpointConfig
from ..engines import ENGINES
from .metrics import RunMetrics


async def run_load_test(
    endpoint_cfg: EndpointConfig,
    profile,
) -> Tuple[List[dict], RunMetrics, List[dict]]:
    """Прогоняет профиль по эндпоинту. Нагрузка идёт только отсюда и только по вызову."""
    engine_cls = ENGINES.get(profile.type)
    if engine_cls is None:
        raise ValueError(f"Неизвестный тип профиля: {profile.type}")
    engine = engine_cls(profile, endpoint_cfg)

    all_results: List[dict] = []
    events: List[dict] = []
    test_start_ts = time.time()
    test_start_mono = time.monotonic()

    async for phase_results, phase_concurrency in engine.generate_phases():
        phase_start_rel = time.time() - test_start_ts
        events.append({
            "event": "phase_start",
            "phase_start_rel": phase_start_rel,
            "concurrency": phase_concurrency,
            "wall_time": time.time(),
        })

        phase_start_wall = time.time()
        for result in phase_results:
            if "request_start_ts" in result:
                result["relative_ts"] = result["request_start_ts"] - test_start_ts
                result["phase_start_rel"] = phase_start_rel
        all_results.extend(phase_results)

        events.append({
            "event": "phase_end",
            "phase_duration": time.time() - phase_start_wall,
            "requests_in_phase": len(phase_results),
        })

    # Монотонные часы: шаг NTP назад на часовом прогоне давал отрицательную
    # длительность, схема объявляет её неотрицательной, и прогон терялся на записи.
    wall_duration = time.monotonic() - test_start_mono
    # Результаты приходят воркер за воркером, то есть порядок — «фаза → воркер →
    # время». Метрики же считают «первую ошибку» и скользящее окно по порядку списка:
    # без этой сортировки ошибка на второй секунде у четвёртого воркера проигрывает
    # ошибке на двухсотой у первого.
    all_results.sort(key=lambda item: item.get("request_start_ts") or 0.0)
    metrics = RunMetrics.from_results(all_results, wall_duration)

    analyze = getattr(engine, "analyze_stress_breakpoint", None)
    if analyze is not None:
        metrics = analyze(all_results, metrics)

    return all_results, metrics, events
