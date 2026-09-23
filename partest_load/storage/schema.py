"""Модели сохраняемого прогона: то, что попадает в JSON и потом в отчёт.

Примеры в схеме — выдуманные. В прототипе в `json_schema_extra` стоял настоящий адрес
стенда заказчика и настоящие идентификаторы сущностей; схема данных — последнее место,
где такое ищут, и первое, откуда оно уезжает вместе с пакетом.
"""

from __future__ import annotations

from datetime import datetime
from typing import Annotated, Any, Dict, List, Literal, Optional

from pydantic import BaseModel, BeforeValidator, ConfigDict, Field, computed_field, field_validator

from ..operation import check_operation

# Условный бюджет ошибок: доля, которую прогон может потратить, оставаясь приемлемым.
ERROR_BUDGET_PCT = 5.0


def parse_float_ts(value: Any) -> float:
    if isinstance(value, str):
        return float(value)
    if isinstance(value, (int, float)):
        return float(value)
    raise ValueError("Ожидается число или строка с числом")


UnixTimestamp = Annotated[float, BeforeValidator(parse_float_ts)]


class RequestResult(BaseModel):
    """Результат одного запроса — то, что вернул воркер."""

    timestamp: str = Field(..., description="ISO8601 UTC")
    request_start_ts: UnixTimestamp = Field(..., description="Unix-время начала запроса")

    worker_id: str
    phase: str = Field(default="", description="Имя фазы расписания")

    url: str
    method: Literal["GET", "POST", "PUT", "PATCH", "DELETE"] = "POST"
    payload: Optional[Dict[str, Any]] = None
    payload_label: str = Field(default="", description="Краткая метка варианта тела")

    status: int = Field(..., ge=0)
    duration_sec: float = Field(..., ge=0.0)
    success: bool
    size_mb: float = Field(..., ge=0.0)

    error: str = Field(default="")
    error_type: Optional[str] = Field(default=None, description="timeout, server_5xx, http_502 …")

    active_concurrency: Optional[int] = Field(default=None, ge=0)
    relative_ts: Optional[float] = Field(default=None, description="Секунды от начала прогона")
    phase_start_rel: Optional[float] = Field(default=None, description="Секунды от начала фазы")

    @field_validator("timestamp")
    @classmethod
    def validate_timestamp(cls, value: str) -> str:
        try:
            datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError as exc:
            raise ValueError(f"Некорректный ISO8601 timestamp: {value}") from exc
        return value

    # Воркер добавляет к результату поля, которых схема не знает (например size_range).
    # Терять их при сохранении хуже, чем пропустить.
    model_config = ConfigDict(extra="allow")


class AggregatedMetrics(BaseModel):
    """Агрегированные метрики прогона."""

    total_requests: int = Field(..., ge=0)
    success_count: int = Field(..., ge=0)
    success_rate: float = Field(..., ge=0.0, le=1.0)
    error_rate_pct: float = Field(..., ge=0.0, le=100.0)

    real_rps: float = Field(..., ge=0.0)
    duration_sec: float = Field(..., ge=0.0)

    p50_ms: float = Field(..., ge=0.0)
    p95_ms: float = Field(..., ge=0.0)
    p99_ms: float = Field(..., ge=0.0)
    max_latency_ms: float = Field(..., ge=0.0)
    mean_latency_ms: float = Field(..., ge=0.0)
    median_latency_ms: Optional[float] = Field(default=None)

    avg_response_mb: float = Field(..., ge=0.0)
    max_response_mb: float = Field(..., ge=0.0)

    errors_by_type: Dict[str, int] = Field(default_factory=dict)

    saturation_point_concurrency: Optional[int] = None
    time_to_first_error_sec: Optional[float] = None
    degradation_slope_pct_per_min: Optional[float] = None
    degradation_status: Literal["stable", "minor", "moderate", "critical"] = "stable"

    @computed_field
    @property
    def error_budget_burned_pct(self) -> float:
        """Сколько процентов условного бюджета ошибок израсходовано."""
        if self.error_rate_pct <= 0:
            return 0.0
        return min(100.0, (self.error_rate_pct / ERROR_BUDGET_PCT) * 100.0)

    @classmethod
    def from_run_metrics(cls, metrics) -> "AggregatedMetrics":
        """Перекладывает `core.metrics.RunMetrics` в сохраняемый вид."""
        return cls(
            total_requests=metrics.total_requests,
            success_count=metrics.success_requests,
            success_rate=metrics.success_rate,
            error_rate_pct=metrics.error_rate_pct,
            real_rps=metrics.real_rps,
            duration_sec=metrics.duration_sec,
            p50_ms=metrics.p50_ms,
            p95_ms=metrics.p95_ms,
            p99_ms=metrics.p99_ms,
            max_latency_ms=metrics.max_ms,
            mean_latency_ms=metrics.mean_ms,
            median_latency_ms=metrics.median_ms,
            avg_response_mb=metrics.avg_response_mb,
            max_response_mb=metrics.max_response_mb,
            errors_by_type=metrics.errors_by_type,
            saturation_point_concurrency=metrics.saturation_point_concurrency,
            time_to_first_error_sec=metrics.time_to_first_error_sec,
            degradation_slope_pct_per_min=metrics.degradation_slope_pct_per_min,
            degradation_status=metrics.degradation_status,
        )


class LoadTestRun(BaseModel):
    """Полный прогон — то, что ложится в JSON.

    Прогон опознаётся **операцией**: `operation` — то, что нагружали, в форме
    «МЕТОД /путь». Это то, чем эндпоинт опознаётся в семействе и в карте; по нему замер
    сводится с узлом графа. `slug` — человекочитаемая подпись того же прогона, она
    показывается в отчёте и ничего не опознаёт: два прогона с разными подписями и одной
    операцией — прогоны одного эндпоинта.

    Операция лежит здесь в исходном виде, а не только в имени каталога: имя каталога
    получается из операции необратимо (см. `operation.operation_key`), и читателю файла
    не должно требоваться ничего расшифровывать.
    """

    format_version: Literal["2.0"] = "2.0"
    run_id: str = Field(..., pattern=r"^\d{8}-\d{6}__[a-z0-9_-]+__[a-z0-9_-]+__[a-f0-9]{8}$")
    operation: str = Field(..., description="Операция прогона: «МЕТОД /путь»")
    slug: str = Field(..., description="Подпись для глаз: имя эндпоинта у потребителя")
    profile_name: str
    started_at: str = Field(..., description="ISO8601 UTC")
    finished_at: Optional[str] = None

    config_snapshot: Dict[str, Any] = Field(default_factory=dict)

    # Сырые результаты включаются только по явной просьбе: на длинном прогоне это
    # сотни мегабайт, которые никто не читает.
    raw_results: Optional[List[RequestResult]] = None

    metrics: AggregatedMetrics
    events: List[Dict[str, Any]] = Field(default_factory=list)

    model_config = ConfigDict(extra="forbid")

    @field_validator("operation")
    @classmethod
    def validate_operation(cls, value: str) -> str:
        return check_operation(value)
