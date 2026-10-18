"""Ступенчатый рост нагрузки до отказа.

Единственный движок, у которого расписание не дописано заранее: лестница уровней
известна, но прогон обрывается там, где система перестала держать. Поэтому здесь три
раздельные вещи: `plan()` — лестница, `base._threshold_breaches` — критерий обрыва (общий
для всех профилей), `saturation_point()` — уровень, на котором система сломалась. Все три
считаются без сети.

**Уровень лестницы поднимает и потолок одновременных запросов, и темп отправки.** На
уровне L темп на воркера — профильный `rate_limit_rps`, умноженный на
`L / initial_concurrency` (`_stress_rate_per_worker`), а предел запросов на воркера — не меньше
доли уровня (`_stress_requests_per_worker`). Первый уровень идёт ровно в профильном темпе;
отношение двух уровней — отношение нагрузок. Без подъёма темпа уровень оставался бы только
потолком, который при быстрых ответах не достигается.

Пороги обрыва — те же, что у остальных профилей (`base._threshold_breaches`), на окне
последних завершившихся запросов.
"""

from __future__ import annotations

import logging
import math
from typing import AsyncGenerator, List, Sequence, Tuple

from ..core.metrics import RunMetrics
from ..core.worker import _latest_finished
from .base import (  # noqa: F401 — RECENT_WINDOW и MIN_SAMPLE_FOR_STOP доступны и отсюда
    MIN_SAMPLE_FOR_STOP,
    RECENT_WINDOW,
    LoadEngine,
    Phase,
    _latency_breached,
    _rate_breached,
    _threshold_breaches,
)

log = logging.getLogger(__name__)

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
    """Причины остановить рост нагрузки по готовым метрикам окна. Пусто — можно дальше.

    Сохранена ради совместимости (была в 0.2.0); движок её не зовёт — лестницу обрывает
    `base._threshold_breaches`. Определения порогов те же (`_rate_breached`,
    `_latency_breached`), так что ответы двух функций на одном окне совпадают.
    """
    reasons: List[str] = []
    if sample_size <= 0:
        return reasons
    if _rate_breached(metrics.error_rate_pct, max_error_rate):
        reasons.append(f"доля ошибок {metrics.error_rate_pct:.1f}%")
    if _latency_breached(metrics.p99_ms, max_p99_ms):
        reasons.append(f"p99 {metrics.p99_ms:.0f} мс")
    server_5xx = metrics.errors_by_type.get("server_5xx", 0)
    if _rate_breached(server_5xx / sample_size * 100, max_5xx_rate):
        reasons.append(f"доля 5xx не ниже {max_5xx_rate}%")
    return reasons


def saturation_point(
    levels: Sequence[Tuple[int, List[dict]]],
    max_error_rate: float,
    min_sample: int = MIN_LEVEL_SAMPLE,
) -> "int | None":
    """Первый уровень параллельности, на котором доля ошибок дошла до порога.

    Целое число уровня или `None`; порог — по тому же определению, что и обрыв лестницы.
    """
    for concurrency, results in levels:
        if len(results) < min_sample:
            continue
        if _rate_breached(error_rate_pct(results), max_error_rate):
            return concurrency
    return None


def _stress_rate_per_worker(profile, level: int) -> float:
    """Темп отправки на воркера на уровне `level`, попыток в секунду.

    Профильный `rate_limit_rps` относится к первому уровню; дальше темп растёт в той же
    пропорции, что и уровень. Отношение двух уровней остаётся отношением нагрузок: уровень
    вдвое выше — вдвое больше попыток в секунду. Разброс пейсинга (`core.pacing`) поверх
    этого прежний: в среднем пауза на 10% длиннее базовой.
    """
    return profile.rate_limit_rps * level / profile.initial_concurrency


def _stress_requests_per_worker(profile, level: int) -> int:
    """Одновременных запросов на воркера на уровне `level`.

    Не меньше профильного `max_requests_per_worker` и не меньше доли уровня на воркера:
    иначе воркеры вместе не могут держать в полёте столько, сколько разрешает уровень, и
    его потолок — фикция.
    """
    return max(profile.max_requests_per_worker, math.ceil(level / profile.workers))


class StressEngine(LoadEngine):
    def __init__(self, profile, endpoint_cfg) -> None:
        super().__init__(profile, endpoint_cfg)
        self.level_results: List[Tuple[int, List[dict]]] = []

    def _level(self, name: str, level: int, duration_sec: float) -> Phase:
        return Phase(name, level, duration_sec,
                     rate_limit_rps=_stress_rate_per_worker(self.profile, level),
                     max_requests_per_worker=_stress_requests_per_worker(self.profile, level))

    def plan_notes(self) -> List[str]:
        notes = super().plan_notes()
        zero = [key for key in ("stop_on_error_rate", "stop_on_5xx_rate")
                if getattr(self.profile, key, None) == 0]
        if zero:
            notes.append(
                f"{', '.join(zero)}: 0 — «есть хоть один случай в окне проверки на конце "
                "уровня», а не «остановиться сразу»: лестница идёт, пока уровень заканчивается "
                "без ошибок (до 0.3 порог 0 обрывал её на первом уровне)")
        return notes

    def plan(self) -> List[Phase]:
        """Лестница уровней без учёта обрыва: столько, сколько разрешают потолок и время."""
        profile = self.profile
        by_time = max(1, int(math.ceil(profile.max_duration_sec / profile.ramp_up_interval_sec)))
        phases: List[Phase] = []
        concurrency = profile.initial_concurrency
        while concurrency <= profile.max_concurrency and len(phases) < by_time:
            phases.append(self._level(f"stress-lvl-{concurrency}", concurrency,
                                      profile.ramp_up_interval_sec))
            concurrency += profile.ramp_up_step
        return phases

    async def generate_phases(self) -> AsyncGenerator[Tuple[List[dict], int], None]:
        """Лестница до первого достигнутого порога, затем удержание на уровне отказа.

        Пороги проверяются на границе уровня. Обрыв лестницы оставляет `stop_reasons` и
        событие `stopped`; порог на последнем уровне лестницы обрывать нечего — событие
        `breached`. Удержание идёт в обоих случаях.
        """
        profile = self.profile
        self.level_results = []
        self.stop_reasons = []
        self.events = []
        thresholds = self._thresholds()
        recent: List[dict] = []
        broken_at = None

        plan = self.plan()
        for index, phase in enumerate(plan):
            phase_results = await self._run_phase(phase)
            self.level_results.append((phase.concurrency, phase_results))
            recent = _latest_finished(recent + phase_results, RECENT_WINDOW)
            self.stop_reasons = _threshold_breaches(recent, min_sample=self._min_sample(),
                                                   **thresholds)
            if self.stop_reasons:
                self._record_breach(phase, cut=index < len(plan) - 1)
                broken_at = phase.concurrency

            yield phase_results, phase.concurrency

            if broken_at is not None:
                break

        if broken_at is not None and profile.hold_after_breaking_sec > 0:
            # Удержание после отказа: видно, восстанавливается ли система, пока нагрузка
            # уровня отказа держится.
            hold = self._level("hold-after-break", broken_at, profile.hold_after_breaking_sec)
            hold_results = await self._run_phase(hold)
            self.level_results.append((broken_at, hold_results))
            yield hold_results, broken_at

    def analyze_stress_breakpoint(self, all_results: List[dict], metrics: RunMetrics) -> RunMetrics:
        """Дописывает в метрики уровень, на котором система перестала держать."""
        metrics.saturation_point_concurrency = saturation_point(
            self.level_results, self.profile.stop_on_error_rate)
        return metrics
