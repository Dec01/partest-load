"""Движки: у каждого профиля своя кривая нагрузки во времени."""

from typing import Dict, Type

from .base import LoadEngine, Phase
from .batch import BatchEngine
from .linear import LinearEngine
from .ramp import RampEngine
from .stress import StressEngine

ENGINES: Dict[str, Type[LoadEngine]] = {
    "linear": LinearEngine,
    "ramp": RampEngine,
    "stress": StressEngine,
    "batch": BatchEngine,
}

__all__ = [
    "ENGINES",
    "BatchEngine",
    "LinearEngine",
    "LoadEngine",
    "Phase",
    "RampEngine",
    "StressEngine",
]
