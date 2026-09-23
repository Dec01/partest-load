"""Схема тестируемого эндпоинта.

В прототипе этот модуль хранил и схему, и сами эндпоинты заказчика — с адресами стендов,
заголовками и учётными данными. В библиотеке остаётся только схема: значения приходят
из файла потребителя, который в пакет не попадает.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Dict, List, Literal, Mapping, Optional, Union

import yaml
from pydantic import BaseModel, ConfigDict, Field, HttpUrl, model_validator

from ..operation import normalize_operation

HttpMethod = Literal["GET", "POST", "PUT", "PATCH", "DELETE"]

# Методы, у которых тело запроса имеет смысл. Для остальных payload остаётся меткой
# варианта (по нему считается ротация), но в запрос не уходит.
BODY_METHODS = frozenset({"POST", "PUT", "PATCH"})


def payload_label(payload: Optional[Dict[str, Any]]) -> str:
    """Метка варианта тела для отчёта: имена полей, без значений.

    Значения не берутся намеренно. Метка ложится в открытый JSON рядом с результатами, а
    что именно потребитель посылает в теле — ни библиотека, ни отчёт знать не должны: там
    бывают и учётные данные, и чужие персональные данные. Какие поля осмысленны, знает
    только тот, чей это контракт, поэтому имена не перечисляются в пакете, а берутся из
    самого тела.
    """
    if payload is None:
        return "без тела"
    if not isinstance(payload, dict):
        return "тело не объект"
    if not payload:
        return "пустое тело"
    names = list(payload)[:5]
    tail = ", …" if len(payload) > len(names) else ""
    return "поля: " + ", ".join(str(name) for name in names) + tail


class PayloadVariant(BaseModel):
    """Один вариант тела запроса. Варианты ротируются по кругу, поровну."""

    body: Optional[Dict[str, Any]] = None


class EndpointConfig(BaseModel):
    """Схема одного эндпоинта под нагрузкой."""

    name: str = Field(..., description="Человеко-читаемое название")
    base_url: HttpUrl = Field(..., description="Базовый URL без пути")
    path: str = Field("", description="Путь, может содержать {placeholders}")
    method: HttpMethod = "POST"
    headers: Dict[str, str] = Field(default_factory=dict)
    query_params: Optional[Dict[str, Union[str, int, float, bool]]] = None
    payloads: List[PayloadVariant] = Field(
        default_factory=lambda: [PayloadVariant(body={})],
        description="Варианты тела запроса (для POST/PUT/PATCH)",
    )
    timeout: float = Field(15.0, ge=1.0, le=120.0)
    batch_mode: bool = Field(default=False, description="Конечный набор запросов вместо потока")
    repeat_count: int = Field(default=1, ge=1, le=100, description="Повторов на URL в batch")

    model_config = ConfigDict(validate_assignment=True, extra="forbid")

    @model_validator(mode="after")
    def _ensure_payloads(self) -> "EndpointConfig":
        if not self.payloads:
            if self.method in BODY_METHODS:
                raise ValueError("Для POST/PUT/PATCH нужен хотя бы один payload")
            # Один пустой вариант: воркеру нужно что-то ротировать, даже когда тела нет.
            object.__setattr__(self, "payloads", [PayloadVariant(body=None)])
        return self

    def url(self) -> str:
        """Полный адрес запроса, собранный из базы и пути."""
        return f"{str(self.base_url).rstrip('/')}/{self.path.lstrip('/')}"

    def operation(self) -> str:
        """Операция прогона: «МЕТОД /путь», то есть опознание узла графа.

        Отдельного аргумента для операции нет и не нужно: метод и путь в описании эндпоинта
        уже есть, а выведенное из них значение не может разойтись с тем, что реально
        нагружали. Аргумент командной строки мог бы — и первым же расхождением сломал бы
        сведение замера с картой.

        Базовый адрес в операцию не входит: один и тот же узел на тесте и на препроде — это
        один узел. Где именно шёл прогон, остаётся в снимке конфигурации.
        """
        return normalize_operation(self.method, self.path)

    def snapshot(self) -> Dict[str, Any]:
        """Описание эндпоинта для файла прогона: имена вместо значений.

        Снимок ложится в открытый JSON рядом с результатами и живёт там месяцами. В
        `headers` у потребителя лежит `Authorization`, в `payloads` — чужие данные; ни то,
        ни другое в артефакт не уезжает. Остаются имена заголовков и метки вариантов: по
        ним читатель отчёта понимает, чем шёл прогон, и не получает ничьего секрета.
        """
        data = self.model_dump(mode="json")
        data.pop("headers", None)
        data.pop("payloads", None)
        data["header_names"] = sorted(self.headers)
        data["payload_labels"] = [payload_label(variant.body) for variant in self.payloads]
        return data


def parse_endpoints(data: Mapping[str, Any]) -> Dict[str, EndpointConfig]:
    """Разбирает отображение «ключ эндпоинта → описание» в проверенные модели."""
    if not isinstance(data, Mapping):
        raise ValueError("Описание эндпоинтов должно быть отображением ключ → описание")
    return {str(key): EndpointConfig.model_validate(value) for key, value in data.items()}


def load_endpoints(path: Union[str, Path]) -> Dict[str, EndpointConfig]:
    """Читает YAML-файл потребителя с описаниями эндпоинтов.

    Путь всегда приходит снаружи: библиотека не знает ни каталога запуска, ни того, где
    потребитель держит свою конфигурацию.
    """
    file = Path(path)
    raw = yaml.safe_load(file.read_text(encoding="utf-8")) or {}
    return parse_endpoints(raw)
