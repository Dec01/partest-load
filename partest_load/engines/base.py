"""Общее для всех движков: фаза, её выполнение и пороги остановки.

Расписание нагрузки отделено от её создания. `plan()` — чистая арифметика: какие уровни
параллельности, в каком порядке и по сколько секунд. `generate_phases()` берёт то же
расписание и отправляет запросы. Разделение сделано ради проверяемости: кривая профиля —
это то, ради чего существует движок, и её надо уметь проверить, не трогая чужой стенд.

Пороги остановки (`stop_on_*` профиля) определены здесь один раз для всех профилей:
`_rate_breached` и `_latency_breached` — что значит «порог достигнут», `_threshold_breaches` —
какие пороги достигнуты на окне последних завершившихся запросов.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from typing import Any, AsyncGenerator, Dict, List, Optional, Sequence, Tuple

from ..configs.endpoints import EndpointConfig
from ..core.metrics import percentile
from ..core.worker import _StopCheck, _latest_finished, launch_phase_workers

log = logging.getLogger(__name__)

# Сколько последних запросов берётся для проверки порогов остановки и сколько их нужно
# накопить, прежде чем пороги вообще применяются. На меньшей выборке одна неудачная
# секунда даёт «отказ» там, где его нет.
RECENT_WINDOW = 300
MIN_SAMPLE_FOR_STOP = 150

# Ключ профиля → имя порога в `_threshold_breaches`.
_THRESHOLD_KEYS = {
    "stop_on_error_rate": "max_error_rate",
    "stop_on_p95_latency_ms": "max_p95_ms",
    "stop_on_p99_latency_ms": "max_p99_ms",
    "stop_on_5xx_rate": "max_5xx_rate",
}


@dataclass(frozen=True)
class Phase:
    """Один отрезок расписания: столько-то параллельных запросов столько-то секунд.

    Необязательные поля перекрывают профиль для этой фазы: `rate_limit_rps` — темп на
    воркера, `max_requests_per_worker` — одновременных запросов на воркера, `requests` —
    конечный список запросов (фаза кончается, когда он пройден). `None` — как в профиле.
    """

    name: str
    concurrency: int
    duration_sec: float
    rate_limit_rps: Optional[float] = None
    max_requests_per_worker: Optional[int] = None
    requests: Optional[int] = None


def _rate_breached(share_pct: float, threshold_pct: Optional[float]) -> bool:
    """Достигнут ли порог доли — одно определение для всех профилей.

    Порог включительный. Срабатывает только при ненулевой доле: порог `0` значит «в окне
    проверки есть хоть один такой случай», а не «остановиться сразу» и не «до первого
    случая вообще» — случай, ушедший из окна до проверки, не учитывается.
    """
    return threshold_pct is not None and share_pct > 0 and share_pct >= threshold_pct


def _latency_breached(value_ms: float, threshold_ms: Optional[float]) -> bool:
    """Достигнут ли порог задержки. Включительно."""
    return threshold_ms is not None and value_ms >= threshold_ms


def _threshold_breaches(
    results: Sequence[dict],
    *,
    max_error_rate: Optional[float] = None,
    max_p95_ms: Optional[float] = None,
    max_p99_ms: Optional[float] = None,
    max_5xx_rate: Optional[float] = None,
    min_sample: int = MIN_SAMPLE_FOR_STOP,
) -> List[str]:
    """Достигнутые пороги на окне `results`. Пусто — можно продолжать.

    Задержка берётся по всем запросам, включая ошибки и таймауты, — как и в метриках
    прогона. Доля 5xx считается от размера окна, а не от числа ошибок.
    """
    if not results or len(results) < min_sample:
        return []
    total = len(results)
    reasons: List[str] = []
    failed = sum(1 for r in results if not r.get("success", False))
    if _rate_breached(failed / total * 100, max_error_rate):
        reasons.append(f"доля ошибок {failed / total * 100:.1f}% (порог {max_error_rate}%)")
    if max_p95_ms is not None or max_p99_ms is not None:
        durations = [r.get("duration_sec", 0.0) * 1000 for r in results]
        p95 = percentile(durations, 95)
        if _latency_breached(p95, max_p95_ms):
            reasons.append(f"p95 {p95:.0f} мс (порог {max_p95_ms:.0f} мс)")
        p99 = percentile(durations, 99)
        if _latency_breached(p99, max_p99_ms):
            reasons.append(f"p99 {p99:.0f} мс (порог {max_p99_ms:.0f} мс)")
    server_5xx = sum(1 for r in results if r.get("error_type") == "server_5xx")
    if _rate_breached(server_5xx / total * 100, max_5xx_rate):
        reasons.append(f"доля 5xx {server_5xx / total * 100:.1f}% (порог {max_5xx_rate}%)")
    return reasons


def _answered(results: Sequence[dict]) -> int:
    """Сколько запросов вернулось: снятые на выдержке фазы (`cancelled`) не в счёт."""
    return sum(1 for r in results if r.get("error_type") != "cancelled")


class LoadEngine:
    """Движок: профиль плюс эндпоинт, из них расписание, из расписания нагрузка.

    После прогона у движка остаются `stop_reasons` (почему остановлен порогом; пусто —
    дошёл до конца) и `events` — события прогона сверх фаз, которые раннер дописывает в
    `events` записи прогона (`{"event": "stopped", ...}`, `{"event": "batch_list", ...}`).
    """

    def __init__(self, profile, endpoint_cfg: EndpointConfig) -> None:
        self.profile = profile
        self.endpoint = endpoint_cfg
        self.stop_reasons: List[str] = []
        self.events: List[Dict[str, Any]] = []

    def plan(self) -> List[Phase]:
        """Расписание фаз. Ничего не отправляет и ничего не ждёт."""
        raise NotImplementedError

    def _thresholds(self) -> Dict[str, float]:
        """Пороги остановки, заданные в профиле, именами `_threshold_breaches`."""
        return {name: getattr(self.profile, key) for key, name in _THRESHOLD_KEYS.items()
                if getattr(self.profile, key, None) is not None}

    def _min_sample(self) -> int:
        """Сколько запросов нужно в окне, прежде чем пороги применяются."""
        return MIN_SAMPLE_FOR_STOP

    def _phase_rate(self, phase: Phase) -> float:
        """Темп фазы на воркера, попыток в секунду."""
        return phase.rate_limit_rps if phase.rate_limit_rps is not None else self.profile.rate_limit_rps

    def _phase_requests_per_worker(self, phase: Phase) -> int:
        """Одновременных запросов на воркера в фазе."""
        if phase.max_requests_per_worker is not None:
            return phase.max_requests_per_worker
        return self.profile.max_requests_per_worker

    def plan_notes(self) -> List[str]:
        """Что в профиле не сработает так, как написано. Считается без сети, до прогона.

        Сейчас одно: порог, который не может сработать, потому что за весь прогон уйдёт
        меньше запросов, чем нужно окну порогов. Оценка сверху — по номинальному темпу.
        """
        thresholds = self._thresholds()
        if not thresholds:
            return []
        most = 0.0
        for phase in self.plan():
            by_tempo = self.profile.workers * self._phase_rate(phase) * phase.duration_sec
            most += min(by_tempo, phase.requests) if phase.requests is not None else by_tempo
        if most >= self._min_sample():
            return []
        keys = ", ".join(key for key, name in _THRESHOLD_KEYS.items() if name in thresholds)
        return [f"пороги {keys} не сработают: за весь прогон уйдёт не больше ~{int(most)} "
                f"запросов, а пороги проверяются с {self._min_sample()}"]

    async def _run_phase(self, phase: Phase, stop_check: Optional[_StopCheck] = None) -> List[dict]:
        profile = self.profile
        delay = 1.0 / self._phase_rate(phase)
        return await launch_phase_workers(
            count=profile.workers,
            concurrency=phase.concurrency,
            endpoint_cfg=self.endpoint,
            phase_duration_sec=phase.duration_sec,
            phase_name=phase.name,
            max_requests_per_worker=self._phase_requests_per_worker(phase),
            rate_limit_delay=delay,
            request_budget=phase.requests,
            stop_check=stop_check,
            stop_window=RECENT_WINDOW,
        )

    def _phase_done(self, phase: Phase, results: List[dict], cut_in_phase: bool) -> None:
        """Крючок после фазы, до того как она отдана раннеру. По умолчанию ничего.

        `cut_in_phase` — порог остановил эту фазу раньше её конца.
        """

    def _record_breach(self, phase: Phase, cut: bool) -> None:
        """Событие порога: `stopped`, если было что оборвать, иначе `breached`.

        Оборвать есть что, если порог остановил фазу до её конца или после неё в плане
        остались фазы. Порог, достигнутый в последней фазе после её конца (или у batch
        короче окна — когда вернулся весь список), ничего не обрывает и только отмечает
        итог.
        """
        kind = "stopped" if cut else "breached"
        log.warning("%s в фазе %s по порогу профиля: %s",
                    "прогон остановлен" if cut else "порог достигнут",
                    phase.name, ", ".join(self.stop_reasons))
        self.events.append({"event": kind, "phase": phase.name,
                            "concurrency": phase.concurrency,
                            "reasons": list(self.stop_reasons)})

    async def generate_phases(self) -> AsyncGenerator[Tuple[List[dict], int], None]:
        """Фазы по расписанию; пороги профиля обрывают расписание.

        Порог проверяется и внутри фазы, раз в секунду, и на её границе — по последним
        `RECENT_WINDOW` завершившимся запросам. Сработал — текущая фаза останавливается
        (уже отправленное дожидается), следующие не начинаются, в `events` ложится
        событие `stopped` (или `breached`, если обрывать было нечего — см. `_record_breach`).
        """
        self.stop_reasons = []
        self.events = []
        thresholds = self._thresholds()
        min_sample = self._min_sample()
        recent: List[dict] = []
        fired_at: List[float] = []

        def check(phase_tail: List[dict]) -> List[str]:
            window = _latest_finished(recent + phase_tail, RECENT_WINDOW)
            reasons = _threshold_breaches(window, min_sample=min_sample, **thresholds)
            if reasons:
                self.stop_reasons = reasons
                fired_at.append(time.monotonic())
            return reasons

        plan = self.plan()
        for index, phase in enumerate(plan):
            fired_at.clear()
            started = time.monotonic()
            results = await self._run_phase(phase, stop_check=check if thresholds else None)
            recent = _latest_finished(recent + results, RECENT_WINDOW)
            cut_in_phase = bool(fired_at) and fired_at[0] - started < phase.duration_sec
            if phase.requests is not None:
                cut_in_phase = cut_in_phase and _answered(results) < phase.requests
            if thresholds and not self.stop_reasons:
                self.stop_reasons = _threshold_breaches(recent, min_sample=min_sample,
                                                       **thresholds)
            self._phase_done(phase, results, cut_in_phase)
            cut = bool(self.stop_reasons) and (cut_in_phase or index < len(plan) - 1)
            if self.stop_reasons:
                self._record_breach(phase, cut)
            yield results, phase.concurrency
            if self.stop_reasons:
                break
