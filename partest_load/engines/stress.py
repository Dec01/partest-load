"""Ступенчатый рост параллельности до отказа.

Единственный движок, у которого расписание не дописано заранее: лестница уровней
известна, но прогон обрывается там, где система перестала держать. Поэтому здесь три
раздельные вещи: `plan()` — лестница, `stop_reasons()` — критерий обрыва,
`saturation_point()` — уровень, на котором система сломалась. Все три считаются без сети.
"""

from __future__ import annotations

import logging
import math
from typing import AsyncGenerator, List, Sequence, Tuple

from ..core.metrics import RunMetrics
from .base import LoadEngine, Phase

log = logging.getLogger(__name__)

# Сколько последних запросов берётся для проверки критериев обрыва и сколько их нужно
# накопить, прежде чем критерии вообще применяются. На меньшей выборке одна неудачная
# секунда даёт «отказ» там, где его нет.
RECENT_WINDOW = 300
MIN_SAMPLE_FOR_STOP = 150

# Минимум запросов на уровне, ниже которого уровень не объявляется точкой насыщения.
MIN_LEVEL_SAMPLE = 20


def error_rate_pct(results: Sequence[dict]) -> float:
    if not results:
        return 0.0
    failed = sum(1 for r in results if not r.get("success", False))
    return failed / len(results) * 100


def stop_reasons(
    metrics: RunMetrics,
    sample_size: int,
    max_error_rate: float,
    max_p99_ms: float,
    max_5xx_rate: float,
) -> List[str]:
    """Причины остановить рост нагрузки. Пусто — можно давать следующий уровень."""
    reasons: List[str] = []
    if sample_size <= 0:
        return reasons
    if metrics.error_rate_pct >= max_error_rate:
        reasons.append(f"доля ошибок {metrics.error_rate_pct:.1f}%")
    if metrics.p99_ms >= max_p99_ms:
        reasons.append(f"p99 {metrics.p99_ms:.0f} мс")
    server_5xx = metrics.errors_by_type.get("server_5xx", 0)
    if server_5xx / sample_size * 100 >= max_5xx_rate:
        reasons.append(f"доля 5xx не ниже {max_5xx_rate}%")
    return reasons


def saturation_point(
    levels: Sequence[Tuple[int, List[dict]]],
    max_error_rate: float,
    min_sample: int = MIN_LEVEL_SAMPLE,
) -> "int | None":
    """Первый уровень параллельности, на котором доля ошибок дошла до порога.

    В прототипе сюда записывалась строка `"approx"` — в поле, объявленное целым числом.
    До отчёта это значение не доезжало: модель прогона его не принимала.
    """
    for concurrency, results in levels:
        if len(results) < min_sample:
            continue
        if error_rate_pct(results) >= max_error_rate:
            return concurrency
    return None


class StressEngine(LoadEngine):
    def __init__(self, profile, endpoint_cfg) -> None:
        super().__init__(profile, endpoint_cfg)
        self.level_results: List[Tuple[int, List[dict]]] = []

    def plan(self) -> List[Phase]:
        """Лестница уровней без учёта обрыва: столько, сколько разрешают потолок и время."""
        profile = self.profile
        by_time = max(1, int(math.ceil(profile.max_duration_sec / profile.ramp_up_interval_sec)))
        phases: List[Phase] = []
        concurrency = profile.initial_concurrency
        while concurrency <= profile.max_concurrency and len(phases) < by_time:
            phases.append(Phase(f"stress-lvl-{concurrency}", concurrency,
                                profile.ramp_up_interval_sec))
            concurrency += profile.ramp_up_step
        return phases

    async def generate_phases(self) -> AsyncGenerator[Tuple[List[dict], int], None]:
        profile = self.profile
        self.level_results = []
        collected: List[dict] = []
        elapsed = 0.0
        broken_at = None

        for phase in self.plan():
            phase_results = await self._run_phase(phase)
            self.level_results.append((phase.concurrency, phase_results))
            collected.extend(phase_results)
            elapsed += phase.duration_sec

            yield phase_results, phase.concurrency

            if len(collected) < MIN_SAMPLE_FOR_STOP:
                continue

            recent = collected[-RECENT_WINDOW:]
            window_sec = min(profile.ramp_up_interval_sec * 3, elapsed)
            reasons = stop_reasons(
                RunMetrics.from_results(recent, window_sec),
                sample_size=len(recent),
                max_error_rate=profile.stop_on_error_rate,
                max_p99_ms=profile.stop_on_p99_latency_ms,
                max_5xx_rate=profile.stop_on_5xx_rate,
            )
            if reasons:
                log.info("предел достигнут на уровне %d: %s",
                         phase.concurrency, ", ".join(reasons))
                broken_at = phase.concurrency
                break

        if broken_at is not None and profile.hold_after_breaking_sec > 0:
            # Удержание после отказа: интересно не только где сломалось, но и
            # восстанавливается ли система, пока нагрузка держится.
            hold = Phase("hold-after-break", broken_at, profile.hold_after_breaking_sec)
            hold_results = await self._run_phase(hold)
            self.level_results.append((broken_at, hold_results))
            yield hold_results, broken_at

    def analyze_stress_breakpoint(self, all_results: List[dict], metrics: RunMetrics) -> RunMetrics:
        """Дописывает в метрики уровень, на котором система перестала держать."""
        metrics.saturation_point_concurrency = saturation_point(
            self.level_results, self.profile.stop_on_error_rate)
        return metrics
