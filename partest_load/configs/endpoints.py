"""Схема тестируемого эндпоинта.

В прототипе этот модуль хранил и схему, и сами эндпоинты заказчика — с адресами стендов,
заголовками и учётными данными. В библиотеке остаётся только схема: значения приходят
из файла потребителя, который в пакет не попадает.
"""

from __future__ import annotations

import re
import warnings
from pathlib import Path
from typing import Any, Dict, List, Literal, Mapping, Optional, Tuple, Union
from urllib.parse import quote, unquote, urlsplit, urlunsplit

import yaml
from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    HttpUrl,
    field_validator,
    model_validator,
)

from ..operation import normalize_operation

HttpMethod = Literal["GET", "POST", "PUT", "PATCH", "DELETE"]

# Методы, у которых тело запроса имеет смысл. Для остальных payload остаётся меткой
# варианта (по нему считается ротация), но в запрос не уходит.
BODY_METHODS = frozenset({"POST", "PUT", "PATCH"})

# Значение `{placeholder}` пути. Строка или целое: путь — это текст, и всё, что сложнее,
# подставилось бы своим `repr`, а не тем, что имели в виду.
_PathValue = Union[str, int]

_PLACEHOLDER = re.compile(r"\{([^{}/]*)\}")


class ConfigKeyWarning(UserWarning):
    """Ключ конфигурации принят, но ведёт себя не так, как можно подумать."""


# Ключи, которые схема эндпоинта принимает, а ядро не читает. Удалить их нельзя: схема
# запрещает неизвестные ключи, и файл, где они стоят, перестал бы разбираться. Молчать тоже
# нельзя — потребитель думает, что настроил поведение, которого нет.
_IGNORED_ENDPOINT_KEYS = {
    "batch_mode": "конечный набор запросов задаётся профилем type: batch, а не эндпоинтом",
    "repeat_count": "число повторов задаётся в профиле type: batch (его repeat_count)",
}


def _path_placeholders(path: str) -> List[str]:
    """Имена `{placeholders}` пути в порядке появления."""
    return _PLACEHOLDER.findall(path or "")


def _is_blank_path_value(value: Any) -> bool:
    """Пустое значение или одни пробелы: у такого значения нет сегмента пути."""
    return value is None or not str(value).strip()


def _path_segment(value: _PathValue) -> str:
    """Значение `{placeholder}` как часть одного сегмента пути.

    Правила те же, что у `partest.tracking._id_segment` (держать их одинаковыми):
    `quote(..., safe="")` кодирует `/`, `?` и `#`, а значение из одних точек кодируется
    тоже — `.` и `..` убирает нормализация адреса, и нагрузка ушла бы на уровень выше
    шаблона, под именем чужой операции. Пустое значение — `ValueError`: сегмента у него нет.
    """
    if _is_blank_path_value(value):
        raise ValueError("пустое значение параметра пути: сегмента у него нет")
    segment = quote(str(value), safe="")
    if segment in (".", ".."):
        segment = segment.replace(".", "%2E")
    return segment


def _fill_path(path: str, values: Mapping[str, _PathValue]) -> str:
    """Подставляет значения в `{placeholders}` пути.

    Значение кодируется сегментом (`_path_segment`): подставленный идентификатор не может
    поменять сам путь. Плейсхолдер без значения — `ValueError`, а не запрос с фигурными
    скобками.
    """
    missing = [name for name in _path_placeholders(path) if name not in values]
    if missing:
        raise ValueError(
            f"в пути {path!r} нет значения для {', '.join('{' + n + '}' for n in missing)}: "
            "задайте его в path_params эндпоинта или варианта"
        )
    return _PLACEHOLDER.sub(lambda m: _path_segment(values[m.group(1)]), path or "")


def _bare_base(url: str) -> str:
    """Адрес без `user:password@`, `?query` и `#fragment`.

    Учётные данные не идут ни в журнал, ни в вывод, ни в файл прогона. Query и fragment
    разбор `base_url` и так отвергает, но `model_copy(update=…)` и `model_construct` разбор
    обходят: без этой обрезки значения запроса попали бы в снимок и `repr`, а путь
    склеился бы внутрь фрагмента.
    """
    parts = urlsplit(url)
    netloc = parts.netloc.rsplit("@", 1)[-1]
    if (netloc, parts.query, parts.fragment) == (parts.netloc, "", "") and "?" not in url             and "#" not in url:
        return url
    return urlunsplit(parts._replace(netloc=netloc, query="", fragment=""))


def _userinfo(url: str) -> Optional[Tuple[str, str]]:
    """Пара (пользователь, пароль) из `user:password@` адреса; `None`, если пары нет.

    Пустые логин и пароль (`https://@host`, `https://:@host`) — тоже `None`: пустая
    Basic-авторизация перекрыла бы явный заголовок `Authorization` описания.
    """
    parts = urlsplit(url)
    if "@" not in parts.netloc:
        return None
    user, _, password = parts.netloc.rsplit("@", 1)[0].partition(":")
    user, password = unquote(user), unquote(password)
    if not user and not password:
        return None
    return user, password


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
    """Один вариант запроса. Варианты ротируются по кругу, поровну.

    `path_params` — значения `{placeholders}` пути для этого варианта; перекрывают общие
    `path_params` эндпоинта. Так задаётся конечный список ресурсов под нагрузкой: один
    вариант — один идентификатор.
    """

    body: Optional[Dict[str, Any]] = None
    path_params: Dict[str, _PathValue] = Field(default_factory=dict)

    # Ошибка разбора не цитирует входные данные: в теле бывают учётные данные, а сообщение
    # об ошибке печатается в терминал и уезжает в журналы CI.
    model_config = ConfigDict(hide_input_in_errors=True)

    @model_validator(mode="after")
    def _full_url_is_not_an_address(self) -> "PayloadVariant":
        if isinstance(self.body, dict) and "full_url" in self.body:
            # Ключ `full_url` в теле — обычное поле тела: адрес собирается только из
            # описания эндпоинта. Имя похоже на адрес, поэтому разбор предупреждает.
            warnings.warn(
                "ключ full_url в теле запроса не меняет адрес и уходит в тело как обычное "
                "поле; адрес задают base_url, path и path_params",
                ConfigKeyWarning,
            )
        return self


class EndpointConfig(BaseModel):
    """Схема одного эндпоинта под нагрузкой."""

    name: str = Field(..., description="Человеко-читаемое название")
    base_url: HttpUrl = Field(..., description="Базовый URL без пути")
    path: str = Field("", description="Путь, может содержать {placeholders}")
    path_params: Dict[str, _PathValue] = Field(
        default_factory=dict,
        description="Значения {placeholders} пути, общие для всех вариантов",
    )
    method: HttpMethod = "POST"
    headers: Dict[str, str] = Field(default_factory=dict)
    query_params: Optional[Dict[str, Union[str, int, float, bool]]] = None
    payloads: List[PayloadVariant] = Field(
        default_factory=lambda: [PayloadVariant(body={})],
        description="Варианты запроса: тело (для POST/PUT/PATCH) и значения параметров пути",
    )
    timeout: float = Field(15.0, ge=1.0, le=120.0)
    # Ядром не читаются — см. _IGNORED_ENDPOINT_KEYS. Оставлены, чтобы старые файлы
    # разбирались, и предупреждают, если заданы.
    batch_mode: bool = Field(default=False, description="Не используется: см. профиль batch")
    repeat_count: int = Field(default=1, ge=1, le=100,
                              description="Не используется: см. профиль batch")

    # `hide_input_in_errors`: ошибка разбора не цитирует входные данные. Без этого неверное
    # описание печатало в stderr значения заголовков (`Authorization`) и тел вариантов.
    model_config = ConfigDict(validate_assignment=True, extra="forbid",
                              hide_input_in_errors=True)

    # Учётные данные `user:password@` базы хранятся в `base_url` как заданы, но наружу
    # адрес выходит только без них: `url()`, `snapshot()` и `repr` снимают их всегда — как
    # бы описание ни получило адрес (разбор, присваивание, `model_copy`, `model_construct`).
    # Пара уходит только в Basic-авторизацию запроса (`basic_auth()`).

    @field_validator("base_url", mode="before")
    @classmethod
    def _base_has_no_query(cls, value: Any) -> Any:
        # Путь клеится к базе строкой: с `?…` или `#…` в базе он ушёл бы внутрь запроса или
        # якоря, а значения параметров — в снимок и вывод. Проверяется сама строка: пустые
        # `?` и `#` нормализация адреса тоже съела бы, а клейка — нет.
        text = str(value)
        if "?" in text or "#" in text:
            raise ValueError("base_url задаётся без ?query и #fragment; параметры запроса — "
                             "в query_params")
        return value

    def basic_auth(self) -> Optional[Tuple[str, str]]:
        """Пара для Basic-авторизации из `user:password@` базы; `None`, если пары нет."""
        return _userinfo(str(self.base_url))

    def __repr_args__(self):
        for name, value in super().__repr_args__():
            yield name, (_bare_base(str(value)) if name == "base_url" else value)

    @model_validator(mode="before")
    @classmethod
    def _warn_ignored_keys(cls, data: Any) -> Any:
        # Предупреждает ключ со значением не по умолчанию: присваивание
        # (`validate_assignment`) зовёт этот валидатор со всеми полями модели, и ключ со
        # значением по умолчанию там есть всегда. Значение по умолчанию и так ничего не
        # настраивает.
        if isinstance(data, Mapping):
            for key, hint in _IGNORED_ENDPOINT_KEYS.items():
                default = cls.model_fields[key].default
                if key in data and data[key] != default:
                    warnings.warn(
                        f"ключ {key!r} в описании эндпоинта не поддерживается и ни на что "
                        f"не влияет: {hint}",
                        ConfigKeyWarning,
                    )
        return data

    @model_validator(mode="after")
    def _ensure_payloads(self) -> "EndpointConfig":
        if not self.payloads:
            if self.method in BODY_METHODS:
                raise ValueError("Для POST/PUT/PATCH нужен хотя бы один payload")
            # Один пустой вариант: воркеру нужно что-то ротировать, даже когда тела нет.
            object.__setattr__(self, "payloads", [PayloadVariant(body=None)])
        self._check_path_params()
        return self

    def _check_path_params(self) -> None:
        """Форма пути и имена `path_params`: проверяется при разборе.

        Лишнее имя в `path_params` — почти всегда опечатка в нужном, поэтому ошибка.
        Нехватка значений здесь не проверяется: описание без значений годится, чтобы
        вывести из него операцию. Перед нагрузкой её проверяет `require_path_params()`.
        """
        names = _path_placeholders(self.path)
        if "" in names:
            raise ValueError(f"в пути {self.path!r} пустой плейсхолдер {{}}")
        rest = _PLACEHOLDER.sub("", self.path)
        if "{" in rest or "}" in rest:
            raise ValueError(f"в пути {self.path!r} непарная фигурная скобка")
        sources = [("эндпоинта", self.path_params)] + [
            (f"варианта {i}", variant.path_params) for i, variant in enumerate(self.payloads, 1)
        ]
        for where, params in sources:
            extra = sorted(set(params) - set(names))
            if extra:
                raise ValueError(
                    f"path_params {where} задаёт {', '.join(extra)}, "
                    f"а в пути {self.path!r} таких плейсхолдеров нет"
                )
            blank = sorted(name for name, value in params.items() if _is_blank_path_value(value))
            if blank:
                raise ValueError(
                    f"path_params {where}: пустое значение для {', '.join(blank)} — "
                    "сегмента пути у него нет, запрос ушёл бы на коллекцию"
                )

    def require_path_params(self) -> None:
        """Каждому `{placeholder}` пути — значение у каждого варианта; иначе `ValueError`.

        Зовётся до нагрузки (командной строкой и `run_load_test`): ошибка находится до
        первого запроса, а не на каждом запросе под нагрузкой. Заодно каждый вариант
        собирается в адрес, так что и сегменты из точек отказывают здесь же.
        """
        names = _path_placeholders(self.path)
        for i, variant in enumerate(self.payloads, 1):
            missing = [n for n in names
                       if n not in variant.path_params and n not in self.path_params]
            if missing:
                raise ValueError(
                    f"в пути {self.path!r} нет значения для "
                    f"{', '.join('{' + n + '}' for n in missing)} у варианта {i}: "
                    "задайте path_params эндпоинта или этого варианта"
                )
            self.url(variant.path_params)

    def url(self, path_params: Optional[Mapping[str, _PathValue]] = None) -> str:
        """Полный адрес запроса: база плюс путь с подставленными `{placeholders}`.

        `path_params` — значения варианта; они перекрывают общие значения эндпоинта.
        """
        path = _fill_path(self.path, {**self.path_params, **(path_params or {})})
        return f"{_bare_base(str(self.base_url)).rstrip('/')}/{path.lstrip('/')}"

    def operation(self) -> str:
        """Операция прогона: «МЕТОД /путь», то есть опознание эндпоинта.

        Отдельного аргумента для операции нет и не нужно: метод и путь в описании эндпоинта
        уже есть, а выведенное из них значение не может разойтись с тем, что реально
        нагружали. Аргумент командной строки мог бы — и первым же расхождением сломал бы
        сведение замера с внешними данными.

        Базовый адрес в операцию не входит: один и тот же эндпоинт на тесте и на препроде —
        это один эндпоинт. Где именно шёл прогон, остаётся в снимке конфигурации.
        """
        return normalize_operation(self.method, self.path)

    def snapshot(self) -> Dict[str, Any]:
        """Описание эндпоинта для файла прогона: имена вместо значений.

        Снимок ложится в открытый JSON рядом с результатами и живёт там месяцами. В
        `headers` у потребителя лежит `Authorization`, в `payloads` — чужие данные, в
        `query_params` бывают ключи API, в `base_url` — `user:password@`. Значения не
        уезжают ни из одного из них. Остаются база без учётных данных, шаблон пути, имена
        заголовков и параметров запроса и метки вариантов: по ним читатель отчёта понимает,
        чем шёл прогон, и не получает ничьего секрета.
        """
        # `base_url` — строкой и отдельно: у описания из `model_construct` там сырая строка,
        # и сериализатор pydantic предупреждал бы «Expected url but got str».
        # Значения заголовков, тел, параметров пути (идентификаторы чужих сущностей) и запроса
        # не берутся вовсе; шаблон пути остаётся.
        data = self.model_dump(mode="json", exclude={"base_url", "headers", "payloads",
                                                     "path_params", "query_params"})
        data["base_url"] = _bare_base(str(self.base_url))
        data["header_names"] = sorted(self.headers)
        data["query_param_names"] = sorted(self.query_params or {})
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
    try:
        raw = yaml.safe_load(file.read_text(encoding="utf-8")) or {}
    except yaml.YAMLError as exc:
        raise ValueError(f"Не разобрался YAML эндпоинтов {_yaml_error_where(file, exc)}") from None
    return parse_endpoints(raw)


def _yaml_error_where(file: Path, exc: yaml.YAMLError) -> str:
    """Где сломался YAML: файл, строка, колонка — без фрагмента файла.

    Текст `YAMLError` цитирует строку вокруг ошибки, а в описании эндпоинта это бывает
    строка с `Authorization`. Поэтому из ошибки берётся только место; исключение не
    сцепляется с исходным (`from None`), чтобы цитата не всплыла и в трассировке.
    """
    mark = getattr(exc, "problem_mark", None)
    if mark is None:
        return f"{file}"
    return f"{file}: строка {mark.line + 1}, колонка {mark.column + 1}"
