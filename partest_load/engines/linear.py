"""Постоянная нагрузка: разгон ступенями → плато → спад ступенями."""

from __future__ import annotations

from typing import List

from .base import LoadEngine, Phase

# С чего начинается разгон и чем заканчивается спад. Не ноль: фаза без запросов
# не измеряет ничего, а пустая пауза посреди прогона выглядит в отчёте как провал.
FLOOR_CONCURRENCY = 5


def _floor(target: int) -> int:
    """Порог разгона, но не выше заказанного.

    С безусловным порогом профиль с целью 2 давал разгон и спад по 5 — то есть спад
    выше плато и 2.5× заказанной нагрузки. Для базового замера на параллельности 1-3
    инструмент мерил не то, что просили, и молча.
    """
    return min(FLOOR_CONCURRENCY, target)


class LinearEngine(LoadEngine):
    def plan(self) -> List[Phase]:
        profile = self.profile
        phases: List[Phase] = []

        if profile.ramp_up_sec > 0:
            steps = max(4, int(profile.ramp_up_sec / 10) + 1)
            step_dur = profile.ramp_up_sec / steps
            delta = (profile.target_concurrency - _floor(profile.target_concurrency)) / (steps - 1)
            for i in range(steps):
                target = min(profile.target_concurrency,
                             int(_floor(profile.target_concurrency) + i * delta))
                phases.append(Phase(f"ramp-up-{i + 1}",
                                    max(_floor(profile.target_concurrency), target), step_dur))

        phases.append(Phase("steady", profile.target_concurrency, profile.duration_sec))

        if profile.ramp_down_sec > 0:
            # Шагов больше, чем на разгоне: резкий сброс нагрузки даёт всплеск в отчёте,
            # который потом читают как деградацию.
            steps = max(5, int(profile.ramp_down_sec / 5) + 1)
            step_dur = profile.ramp_down_sec / steps
            delta = (profile.target_concurrency - _floor(profile.target_concurrency)) / (steps - 1)
            for i in range(steps):
                target = max(_floor(profile.target_concurrency),
                             int(profile.target_concurrency - i * delta))
                phases.append(Phase(f"ramp-down-{i + 1}", target, step_dur))

        return phases
