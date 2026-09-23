"""Кривая из фаз: разогрев, рост, плато, спад — как написано в профиле."""

from __future__ import annotations

from typing import List

from .base import LoadEngine, Phase

# Целевая длина подшага перегона. Короче — ступеньки видны в отчёте как пила,
# длиннее — переход между уровнями сливается в одну точку.
SUBSTEP_TARGET_SEC = 15
MIN_SUBSTEPS = 5


class RampEngine(LoadEngine):
    def plan(self) -> List[Phase]:
        phases: List[Phase] = []

        for idx, spec in enumerate(self.profile.phases, 1):
            name = spec.name or f"phase-{idx}"

            if not spec.is_ramp:
                phases.append(Phase(name, spec.target_concurrency, spec.duration_sec))
                continue

            steps = max(MIN_SUBSTEPS, int(spec.duration_sec / SUBSTEP_TARGET_SEC) + 1)
            step_dur = spec.duration_sec / steps
            delta = (spec.end_concurrency - spec.start_concurrency) / max(1, steps - 1)
            ceiling = max(spec.start_concurrency, spec.end_concurrency)

            for s in range(steps):
                target = int(spec.start_concurrency + s * delta)
                phases.append(Phase(f"{name}-step{s + 1}",
                                    max(1, min(target, ceiling)), step_dur))

        return phases
