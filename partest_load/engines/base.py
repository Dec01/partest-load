"""Общее для всех движков: фаза и её выполнение.

Расписание нагрузки отделено от её создания. `plan()` — чистая арифметика: какие уровни
параллельности, в каком порядке и по сколько секунд. `generate_phases()` берёт то же
расписание и отправляет запросы. Разделение сделано ради проверяемости: кривая профиля —
это то, ради чего существует движок, и её надо уметь проверить, не трогая чужой стенд.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import AsyncGenerator, List, Tuple

from ..configs.endpoints import EndpointConfig
from ..core.worker import launch_phase_workers


@dataclass(frozen=True)
class Phase:
    """Один отрезок расписания: столько-то параллельных запросов столько-то секунд."""

    name: str
    concurrency: int
    duration_sec: float


class LoadEngine:
    """Движок: профиль плюс эндпоинт, из них расписание, из расписания нагрузка."""

    def __init__(self, profile, endpoint_cfg: EndpointConfig) -> None:
        self.profile = profile
        self.endpoint = endpoint_cfg

    def plan(self) -> List[Phase]:
        """Расписание фаз. Ничего не отправляет и ничего не ждёт."""
        raise NotImplementedError

    async def _run_phase(self, phase: Phase) -> List[dict]:
        return await launch_phase_workers(
            count=self.profile.workers,
            concurrency=phase.concurrency,
            endpoint_cfg=self.endpoint,
            phase_duration_sec=phase.duration_sec,
            phase_name=phase.name,
            max_requests_per_worker=self.profile.max_requests_per_worker,
            rate_limit_delay=self.profile.rate_limit_delay,
        )

    async def generate_phases(self) -> AsyncGenerator[Tuple[List[dict], int], None]:
        for phase in self.plan():
            yield await self._run_phase(phase), phase.concurrency
