"""Конечный набор запросов на одном уровне параллельности.

Единственный профиль, где важно не «сколько времени держим», а «успели ли пройти
весь список». `duration_sec` здесь предельное время, а не длительность нагрузки.
"""

from __future__ import annotations

from typing import List

from ..configs.endpoints import EndpointConfig
from .base import LoadEngine, Phase


class BatchEngine(LoadEngine):
    def __init__(self, profile, endpoint_cfg: EndpointConfig) -> None:
        super().__init__(profile, endpoint_cfg)
        # Прототип менял чужую модель на месте: `setattr(endpoint, ...)`. Копия честнее —
        # эндпоинт может быть общим для нескольких прогонов в одном процессе.
        self.endpoint = endpoint_cfg.model_copy(update={
            "batch_mode": True,
            "repeat_count": profile.repeat_count,
        })

    def plan(self) -> List[Phase]:
        return [Phase("batch", self.profile.target_concurrency, self.profile.duration_sec)]
