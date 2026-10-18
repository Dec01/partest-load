"""Конечный набор запросов на одном уровне параллельности.

Единственный профиль, где важно не «сколько времени держим», а «успели ли пройти
весь список». `duration_sec` здесь предельное время, а не длительность нагрузки.

Список — варианты запроса эндпоинта (`payloads`), каждый повторённый `repeat_count` раз из
профиля. Фаза кончается, когда список пройден. Пройден ли он, считается по числу
вернувшихся замеров — не отправленных и не снятых на выдержке (`cancelled`); итог ложится
событием `batch_list` в `events` прогона с причиной, если не пройден: порог профиля или
предел времени.
"""

from __future__ import annotations

import logging
from typing import List

from .base import MIN_SAMPLE_FOR_STOP, LoadEngine, Phase, _answered

log = logging.getLogger(__name__)


class BatchEngine(LoadEngine):
    def _batch_size(self) -> int:
        """Сколько запросов в списке: варианты × повторы."""
        return max(1, len(self.endpoint.payloads)) * self.profile.repeat_count

    def _min_sample(self) -> int:
        """Порог применяется и к короткому списку: окно не больше самого списка."""
        return min(MIN_SAMPLE_FOR_STOP, self._batch_size())

    def plan(self) -> List[Phase]:
        return [Phase("batch", self.profile.target_concurrency, self.profile.duration_sec,
                      requests=self._batch_size())]

    def plan_notes(self) -> List[str]:
        notes = super().plan_notes()
        if self._thresholds() and self._batch_size() < MIN_SAMPLE_FOR_STOP:
            notes.append(
                f"список batch ({self._batch_size()} запросов) короче окна порогов "
                f"({MIN_SAMPLE_FOR_STOP}): порог проверяется, когда вернулся весь список, и "
                "не обрывает его, а отмечает итог (событие breached)")
        return notes

    def _phase_done(self, phase: Phase, results: List[dict], cut_in_phase: bool) -> None:
        planned = phase.requests or 0
        completed = _answered(results)
        reason = None
        if completed < planned:
            if cut_in_phase:
                reason = "порог профиля: " + ", ".join(self.stop_reasons)
            else:
                reason = f"предел времени duration_sec ({phase.duration_sec:g} с)"
            log.warning("список batch не пройден: вернулось %d из %d (%s)",
                        completed, planned, reason)
        self.events.append({"event": "batch_list", "phase": phase.name, "planned": planned,
                            "completed": completed, "reason": reason})
