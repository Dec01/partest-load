"""Хранение прогонов: схема сохраняемых данных и запись на диск.

Корень результатов всегда приходит снаружи. Библиотека не выбирает, куда писать: каталог
запуска ей не принадлежит.

Прогон опознаётся операцией — `partest_load.operation`. Раскладка каталогов, имя прогона и
указатели на последний прогон ключуются ею же.
"""

from .result_store import (
    validate_run_names,
    SavedRun,
    ResultStoreError,
    get_latest_run_path,
    iter_latest_runs,
    latest_pointer_path,
    resolve_pointer,
    run_history,
    runs_dir,
    sample_raw_results,
    save_run_results,
)
from .schema import AggregatedMetrics, LoadTestRun, RequestResult

__all__ = [
    "validate_run_names",
    "AggregatedMetrics",
    "LoadTestRun",
    "RequestResult",
    "ResultStoreError",
    "SavedRun",
    "get_latest_run_path",
    "iter_latest_runs",
    "latest_pointer_path",
    "resolve_pointer",
    "run_history",
    "runs_dir",
    "sample_raw_results",
    "save_run_results",
]
