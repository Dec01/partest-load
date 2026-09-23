"""Сохранение прогона на диск и указатель на последний результат.

Раскладка ключуется **операцией** прогона (`operation`, см. `partest_load/operation.py`), а не
слагом, который потребитель придумал сам: каталог результатов должен сводиться с картой, а слаг
`orders-create` не сводится с узлом `GET /v1/items` ничем, кроме догадки. Два прогона одной
операции с разными подписями ложатся в одно место; два разных эндпоинта с одной подписью —
в разные.

Имя каталога получается из операции необратимо, поэтому операция пишется внутрь файла прогона
в исходном виде: читателю результата не нужно ничего расшифровывать.

Три отличия от прототипа, каждое вынужденное.

Корень результатов был константой модуля (`Path("results")`), и каталог создавался прямо
на импорте — то есть `import storage.result_store` уже писал на диск, в текущий каталог
процесса. Библиотека не знает, откуда её запустили; корень приходит аргументом.

«Последний прогон» отмечался симлинком. На Windows без прав на создание символических
ссылок это исключение — уже после того, как результат записан. Симлинк по-прежнему
первый выбор, но при отказе рядом ложится файл-указатель, и читатель понимает оба вида.

Печать заменена на журнал: под нагрузкой stdout библиотеки нечем ни выключить, ни
перенаправить.
"""

from __future__ import annotations

import json
import logging
import os
import random
import re
import tempfile
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterator, List, Optional, Sequence, Tuple, Union

from .schema import AggregatedMetrics, LoadTestRun, RequestResult
from ..operation import check_operation, operation_key

log = logging.getLogger(__name__)

# Каталог с указателями на последний прогон каждой пары «операция + профиль».
LATEST_DIRNAME = "_latest"

# Метка файла-указателя. Нужна, чтобы отличить его от настоящего прогона: имя у них
# одинаковое, а читатель один и тот же.
POINTER_MARKER = "partest-load/latest-pointer"

# Выше этого числа сырые результаты прореживаются: ошибки остаются все, успешные — доля.
SAMPLE_THRESHOLD = 50_000
SAMPLE_SUCCESS_RATE = 0.2

# Из чего складывается run_id. Схема прогона проверяет его шаблоном, и подпись с заглавной
# буквой роняет сохранение уже после прогона — поэтому проверяем до, и понятным текстом.
_SLUG_RE = re.compile(r"^[a-z0-9_-]+$")


class ResultStoreError(ValueError):
    """Результат не сохранить: негодная операция, подпись, профиль или каталог."""


@dataclass(frozen=True)
class SavedRun:
    """Что получилось из сохранения: идентификатор прогона и файл с ним."""

    run_id: str
    path: Path


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _iso(moment: datetime) -> str:
    return moment.isoformat().replace("+00:00", "Z")


def _check_slug(value: str, what: str) -> str:
    if not _SLUG_RE.match(value or ""):
        raise ResultStoreError(
            f"{what} должен состоять из строчных букв, цифр, дефиса и подчёркивания; "
            f"получено: {value!r}"
        )
    return value


def _check_operation(value: str) -> str:
    """Операция прогона в том же виде ошибки, что и остальные проверки записи."""
    try:
        return check_operation(value)
    except ValueError as exc:
        raise ResultStoreError(str(exc)) from None


def validate_run_names(*, operation: str, slug: str, profile_name: str) -> None:
    """Проверить опознание прогона до того, как он начнётся.

    Те же правила, что применяет запись, но вызываемые отдельно. Запись происходит после
    прогона, и проверка там означала бы: двадцать минут нагрузки, затем отказ и потерянный
    результат. Вызывающему стоит звать это рядом с разбором аргументов.

    Все три имени обязательны и передаются по имени: операция — личность прогона, и вызов,
    который её забыл, должен не собраться, а не записать прогон «куда-нибудь».
    """
    _check_operation(operation)
    _check_slug(slug, "подпись эндпоинта")
    _check_slug(profile_name, "имя профиля")


def sample_raw_results(
    results: Sequence[dict],
    threshold: int = SAMPLE_THRESHOLD,
    success_rate: float = SAMPLE_SUCCESS_RATE,
    rng: Optional[random.Random] = None,
) -> List[dict]:
    """Прореживает сырые результаты длинного прогона.

    Ошибки сохраняются все до одной: ради них файл и открывают. Успешные берутся долей —
    их сотни тысяч, и на форму кривой одна пятая влияет незаметно.
    """
    if len(results) <= threshold:
        return list(results)
    source = rng if rng is not None else random
    kept = [r for r in results
            if not r.get("success", True) or source.random() < success_rate]
    log.info("сырые результаты прорежены: %d → %d (все ошибки + ~%d%% успешных)",
             len(results), len(kept), int(success_rate * 100))
    return kept


def runs_dir(results_root: Union[str, Path], operation: str, profile_name: str) -> Path:
    """Каталог прогонов пары «операция + профиль»: `<корень>/<ключ операции>/<профиль>`."""
    return Path(results_root) / operation_key(_check_operation(operation)) / profile_name


def latest_pointer_path(results_root: Union[str, Path],
                        operation: str, profile_name: str) -> Path:
    """Файл-указатель на последний прогон пары «операция + профиль»."""
    key = operation_key(_check_operation(operation))
    return Path(results_root) / LATEST_DIRNAME / f"{key}__{profile_name}.json"


def _write_pointer(pointer: Path, target: Path) -> None:
    """Отмечает последний прогон: симлинком, а если нельзя — файлом-указателем."""
    pointer.parent.mkdir(parents=True, exist_ok=True)
    if pointer.exists() or pointer.is_symlink():
        pointer.unlink()

    relative = Path(os.path.relpath(target, pointer.parent)).as_posix()
    try:
        pointer.symlink_to(relative)
        return
    except (OSError, NotImplementedError) as exc:
        # Windows без «режима разработчика» и часть сетевых файловых систем симлинки
        # не дают. Терять уже посчитанный прогон из-за этого нельзя.
        log.debug("симлинк недоступен (%s), пишем файл-указатель", type(exc).__name__)

    pointer.write_text(
        json.dumps({"format": POINTER_MARKER, "points_to": relative}, ensure_ascii=False),
        encoding="utf-8",
    )


def resolve_pointer(pointer: Path) -> Optional[Path]:
    """Куда указывает отметка последнего прогона. `None`, если цели нет."""
    if pointer.is_symlink():
        try:
            resolved = pointer.resolve(strict=True)
        except (OSError, RuntimeError):
            return None
        return resolved if resolved.is_file() else None

    if not pointer.is_file():
        return None
    try:
        data = json.loads(pointer.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    if isinstance(data, dict) and data.get("format") == POINTER_MARKER:
        target = (pointer.parent / str(data.get("points_to", ""))).resolve()
        return target if target.is_file() else None
    # Не указатель, а настоящий прогон, положенный сюда руками.
    return pointer


def get_latest_run_path(results_root: Union[str, Path],
                        operation: str, profile_name: str) -> Optional[Path]:
    """Путь к самому свежему прогону пары или `None`, если его нет."""
    return resolve_pointer(latest_pointer_path(results_root, operation, profile_name))


def iter_latest_runs(results_root: Union[str, Path]) -> Iterator[Tuple[str, str, Path]]:
    """Перебирает последние прогоны: «ключ операции, профиль, файл».

    Отчёт строится по этому перечислению, а не по своему обходу каталогов: где лежит
    результат и чем отмечен последний, знает хранилище.

    Отдаётся **ключ** операции, а не сама операция: из имени указателя её не восстановить, а
    читать ради неё каждый файл прогона нельзя — с сырыми результатами это сотни мегабайт,
    и читатель всё равно откроет файл сам. Сама операция лежит в файле, в поле `operation`.
    """
    latest_dir = Path(results_root) / LATEST_DIRNAME
    if not latest_dir.is_dir():
        return
    for pointer in sorted(latest_dir.glob("*__*.json")):
        target = resolve_pointer(pointer)
        if target is None:
            log.warning("указатель %s ведёт в никуда", pointer)
            continue
        key, _, profile_name = pointer.stem.partition("__")
        yield key, profile_name, target


def run_history(results_root: Union[str, Path],
                operation: str, profile_name: str) -> List[Path]:
    """Все прогоны пары, от свежего к старому. Порядок — по метке времени в имени."""
    folder = runs_dir(results_root, operation, profile_name)
    if not folder.is_dir():
        return []
    return sorted((p for p in folder.glob("*.json") if p.is_file()),
                  key=lambda p: p.name.split("__")[0], reverse=True)


def save_run_results(
    results_root: Union[str, Path],
    *,
    operation: str,
    slug: str,
    profile_name: str,
    metrics: AggregatedMetrics,
    events: List[dict],
    raw_results: Optional[List[dict]] = None,
    config_snapshot: Optional[Dict[str, Any]] = None,
    save_raw: bool = False,
    rng: Optional[random.Random] = None,
) -> SavedRun:
    """Пишет прогон в `<корень>/<ключ операции>/<профиль>/<run_id>.json`.

    Операция и подпись передаются только по имени: перепутать их местами было бы легко, а
    последствие — прогон, разложенный по слагу, то есть ровно то, от чего раскладку и
    перевели на операцию.

    Запись атомарная: сначала временный файл в том же каталоге, потом переименование.
    Оборванный на середине процесс не оставляет полупрогона, который отчёт примет за
    настоящий.
    """
    operation = _check_operation(operation)
    _check_slug(slug, "подпись эндпоинта")
    _check_slug(profile_name, "имя профиля")

    started = _utc_now()
    key = operation_key(operation)
    run_id = f"{started.strftime('%Y%m%d-%H%M%S')}__{key}__{profile_name}__{uuid.uuid4().hex[:8]}"

    folder = runs_dir(results_root, operation, profile_name)
    folder.mkdir(parents=True, exist_ok=True)
    file_path = folder / f"{run_id}.json"

    kept_raw: Optional[List[dict]] = None
    if save_raw and raw_results:
        kept_raw = [RequestResult.model_validate(r)
                    for r in sample_raw_results(raw_results, rng=rng)]

    run_data = LoadTestRun(
        run_id=run_id,
        operation=operation,
        slug=slug,
        profile_name=profile_name,
        started_at=_iso(started),
        finished_at=_iso(_utc_now()),
        config_snapshot=config_snapshot or {},
        raw_results=kept_raw,
        metrics=metrics,
        events=events,
    )
    payload = run_data.model_dump(mode="json", exclude_none=True, by_alias=False)

    with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", delete=False,
                                     dir=folder, prefix=f"{run_id}.tmp.") as tmp:
        json.dump(payload, tmp, ensure_ascii=False, indent=2)
        tmp_path = tmp.name
    os.replace(tmp_path, file_path)

    _write_pointer(latest_pointer_path(results_root, operation, profile_name), file_path)
    log.info("прогон сохранён: %s", file_path)
    return SavedRun(run_id=run_id, path=file_path)
