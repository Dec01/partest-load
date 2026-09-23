"""Схемы того, что описывает потребитель: эндпоинт под нагрузкой и профиль нагрузки.

В пакете лежат **схемы**, а не значения. Конкретные эндпоинты, адреса стендов и числа
нагрузки — свойства чужой системы, и их место в конфигурации потребителя.
"""

from .endpoints import EndpointConfig, PayloadVariant, load_endpoints, parse_endpoints
from .profiles import (
    BatchProfile,
    LinearProfile,
    LoadProfile,
    ProfileError,
    RampPhase,
    RampProfile,
    StressProfile,
    load_profile,
    parse_profile,
)

__all__ = [
    "BatchProfile",
    "EndpointConfig",
    "LinearProfile",
    "LoadProfile",
    "PayloadVariant",
    "ProfileError",
    "RampPhase",
    "RampProfile",
    "StressProfile",
    "load_endpoints",
    "load_profile",
    "parse_endpoints",
    "parse_profile",
]
