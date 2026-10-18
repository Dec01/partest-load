"""Схема профиля нагрузки.

Профиль — это данные: он описывает форму нагрузки во времени и живёт у потребителя,
рядом с его системой. Пакет знает только формат и умеет его проверить.

Формат YAML сохранён ровно таким, каким он был в прототипе: те же ключи, те же значения
по умолчанию. Изменилось одно — неизвестный ключ теперь ошибка, а не молчаливое
игнорирование. Профиль, в котором `ramp_up_step` написан как `ramp_step`, раньше работал
с чужими значениями по умолчанию и молчал об этом.
"""

from __future__ import annotations

from pathlib import Path
from typing import Annotated, Any, Literal, Mapping, Optional, Union

import yaml
from pydantic import BaseModel, ConfigDict, Field, TypeAdapter, ValidationError, model_validator

from .endpoints import _yaml_error_where


class ProfileError(ValueError):
    """Профиль не разобрался: неизвестный тип, битый YAML или неверные значения."""


class _Profile(BaseModel):
    """Общая часть любого профиля."""

    model_config = ConfigDict(extra="forbid", hide_input_in_errors=True)

    name: str = ""
    workers: int = Field(..., ge=1, description="Сколько корутин отправляют запросы")
    max_requests_per_worker: int = Field(30, ge=1, description="Одновременных запросов на воркера")
    rate_limit_rps: float = Field(10.0, gt=0.0, description="Попыток отправки в секунду на воркера")
    stop_on_error_rate: Optional[float] = Field(None, ge=0.0, le=100.0)

    @property
    def rate_limit_delay(self) -> float:
        """Базовая пауза между попытками отправки. Разброс добавляет `core.pacing`."""
        return 1.0 / self.rate_limit_rps


class LinearProfile(_Profile):
    """Постоянная нагрузка: разгон → плато → (необязательно) спад."""

    type: Literal["linear"]
    duration_sec: float = Field(300, gt=0.0)
    target_concurrency: int = Field(50, ge=1)
    workers: int = Field(6, ge=1)
    ramp_up_sec: float = Field(30, ge=0.0)
    ramp_down_sec: float = Field(0, ge=0.0)


class RampPhase(BaseModel):
    """Одна фаза ramp-профиля: либо фиксированный уровень, либо перегон между уровнями."""

    model_config = ConfigDict(extra="forbid", hide_input_in_errors=True)

    name: str = ""
    duration_sec: float = Field(60, gt=0.0)
    target_concurrency: Optional[int] = Field(None, ge=1)
    start_concurrency: Optional[int] = Field(None, ge=1)
    end_concurrency: Optional[int] = Field(None, ge=1)
    mode: Literal["linear"] = "linear"

    @model_validator(mode="after")
    def _ends_go_together(self) -> "RampPhase":
        has_start = self.start_concurrency is not None
        has_end = self.end_concurrency is not None
        if has_start != has_end:
            # Раньше фаза с одним концом молча превращалась в фазу с фиксированным
            # уровнем, и профиль давал не ту кривую, которую в нём написали.
            raise ValueError(
                "start_concurrency и end_concurrency задаются только вместе"
            )
        if not has_start and self.target_concurrency is None:
            raise ValueError(
                "фазе нужен либо target_concurrency, либо пара start_concurrency/end_concurrency"
            )
        return self

    @property
    def is_ramp(self) -> bool:
        return self.start_concurrency is not None and self.end_concurrency is not None


class RampProfile(_Profile):
    """Заданная фазами кривая: разогрев, рост, плато, спад."""

    type: Literal["ramp"]
    workers: int = Field(8, ge=1)
    phases: list[RampPhase] = Field(default_factory=list)
    stop_on_p95_latency_ms: Optional[float] = Field(None, gt=0.0)

    @model_validator(mode="after")
    def _phases_not_empty(self) -> "RampProfile":
        if not self.phases:
            raise ValueError("Для ramp-профиля обязательно указать список phases")
        return self


class StressProfile(_Profile):
    """Ступенчатый рост до отказа."""

    type: Literal["stress"]
    workers: int = Field(10, ge=1)
    initial_concurrency: int = Field(50, ge=1)
    max_concurrency: int = Field(600, ge=1)
    ramp_up_step: int = Field(40, ge=1)
    ramp_up_interval_sec: float = Field(20, gt=0.0)
    stop_on_error_rate: float = Field(8.0, ge=0.0, le=100.0)
    stop_on_p99_latency_ms: float = Field(18000, gt=0.0)
    stop_on_5xx_rate: float = Field(2.0, ge=0.0, le=100.0)
    max_duration_sec: float = Field(1200, gt=0.0)
    hold_after_breaking_sec: float = Field(60, ge=0.0)

    @model_validator(mode="after")
    def _ladder_goes_up(self) -> "StressProfile":
        if self.max_concurrency < self.initial_concurrency:
            raise ValueError("max_concurrency не может быть меньше initial_concurrency")
        return self


class BatchProfile(_Profile):
    """Конечный набор запросов на одном уровне параллельности."""

    type: Literal["batch"]
    duration_sec: float = Field(600, gt=0.0)
    target_concurrency: int = Field(30, ge=1)
    workers: int = Field(1, ge=1)
    repeat_count: int = Field(3, ge=1, le=100)


LoadProfile = Annotated[
    Union[LinearProfile, RampProfile, StressProfile, BatchProfile],
    Field(discriminator="type"),
]

_ADAPTER: TypeAdapter = TypeAdapter(LoadProfile)

PROFILE_TYPES = ("linear", "ramp", "stress", "batch")


def parse_profile(data: Mapping[str, Any]) -> Union[LinearProfile, RampProfile, StressProfile, BatchProfile]:
    """Разбирает и проверяет профиль. Тип выбирается по полю `type`."""
    if not isinstance(data, Mapping):
        raise ProfileError("Профиль должен быть отображением ключ → значение")
    if "type" not in data:
        raise ProfileError(f"В профиле нет поля type; известные: {', '.join(PROFILE_TYPES)}")
    try:
        return _ADAPTER.validate_python(dict(data))
    except ValidationError as exc:
        # Место и текст каждой ошибки, без входных значений: корневой адаптер объединения
        # профилей `hide_input_in_errors` моделей не наследует, и `str(exc)` цитировал бы
        # значения. Исходная ошибка не сцепляется по той же причине.
        raise ProfileError(_errors_text(exc)) from None


def _errors_text(exc: ValidationError) -> str:
    lines = []
    for error in exc.errors(include_input=False, include_url=False):
        where = ".".join(str(part) for part in error.get("loc", ())) or "профиль"
        if error.get("type") in ("union_tag_invalid", "union_tag_not_found"):
            # Сообщение pydantic о теге цитирует его значение («Input tag '…'»).
            lines.append(f"type: неизвестный тип профиля; известные: {', '.join(PROFILE_TYPES)}")
            continue
        lines.append(f"{where}: {error.get('msg', '')}")
    return f"профиль не прошёл проверку ({len(lines)}): " + "; ".join(lines)


def load_profile(path: Union[str, Path]) -> Union[LinearProfile, RampProfile, StressProfile, BatchProfile]:
    """Читает и проверяет YAML-профиль по явно указанному пути."""
    file = Path(path)
    try:
        raw = yaml.safe_load(file.read_text(encoding="utf-8"))
    except yaml.YAMLError as exc:
        # Только место ошибки: текст YAMLError цитирует строку файла.
        raise ProfileError(f"Не разобрался YAML профиля {_yaml_error_where(file, exc)}") from None
    if raw is None:
        raise ProfileError(f"Профиль пуст: {file}")
    return parse_profile(raw)
