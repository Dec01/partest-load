"""Сборка HTML-дашборда из сохранённых прогонов.

Что изменилось против прототипа, и почему без этого нельзя.

**Ни одной ссылки наружу.** Прототип тянул plotly и шрифты с CDN. Заказчик работает в
закрытом контуре: там такой отчёт открывается пустой страницей. Теперь plotly.js
вшивается в файл (из установленного пакета `plotly` или из указанного файла), иконочный
шрифт убран совсем. Если plotly.js взять негде — дашборд собирается с таблицами и без
графиков и честно об этом пишет.

**Список эндпоинтов не берётся из кода.** Прототип импортировал `configs.endpoints.ENDPOINTS`
— модуль с эндпоинтами заказчика. Отчёт строится по тому, что лежит в каталоге результатов;
человеческие названия можно передать, но и без них дашборд собирается.

**Прогоны группируются по операции** (`operation` — «МЕТОД /путь»), а не по слагу,
которым потребитель назвал эндпоинт: слаг — подпись, а не личность. Операция читается из
самого файла прогона; в имени каталога лежит только её необратимый ключ.

**Настройки профиля берутся из самого прогона** (`config_snapshot`), а не подбираются по
трём вариантам имени файла в `configs/profiles` относительно текущего каталога. Отчёт о
прогоне месячной давности показывает те настройки, с которыми он шёл, а не сегодняшние.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime
from pathlib import Path
from statistics import mean
from typing import Any, Dict, List, Mapping, Optional, Tuple, Union

from jinja2 import Environment, FileSystemLoader, select_autoescape

from ..storage.result_store import ResultStoreError, iter_latest_runs, run_history
from .charts import create_error_heatmap_config, create_plotly_config, prepare_timeseries_data

log = logging.getLogger(__name__)

TEMPLATES_DIR = Path(__file__).parent / "templates"

# Пороги словесной оценки в сводке. Это оформление отчёта, а не свойство системы:
# у каждого стенда «нормально» своё. Числа перенесены из прототипа.
BAD_ERROR_RATE_PCT = 10.0
WARN_P95_MS = 8000.0
GOOD_P95_MS = 2500.0
NOTABLE_ERROR_RATE_PCT = 5.0

# Сколько крайних замеров с каждого края берётся, когда считается, насколько просела
# скорость ответа внутри одного диапазона размеров.
DEGRADATION_MIN_SAMPLE = 3


def datetimeformat(value: str, fmt: str = "%H:%M %d.%m.%Y") -> str:
    """Фильтр Jinja: ISO8601 → человеческий вид. Непонятное значение отдаётся как есть."""
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00")).strftime(fmt)
    except ValueError:
        return value


def _environment() -> Environment:
    env = Environment(
        loader=FileSystemLoader(str(TEMPLATES_DIR)),
        autoescape=select_autoescape(["html", "j2"]),
    )
    env.filters["datetimeformat"] = datetimeformat
    return env


def plotly_bundle(source: Union[str, Path, None] = None) -> Optional[str]:
    """Содержимое plotly.js для вшивания в отчёт.

    Порядок: явно указанный файл, потом установленный пакет `plotly`. Ссылку на сеть не
    возвращаем никогда — отчёт обязан открываться в контуре без интернета.
    """
    if source is not None:
        return Path(source).read_text(encoding="utf-8")
    try:
        from plotly.offline import get_plotlyjs
    except ImportError:
        log.warning("plotly не установлен и файл plotly.js не указан — дашборд без графиков "
                    "(pip install 'partest-load[report]')")
        return None
    return get_plotlyjs()


def previous_metrics(results_root: Path, operation: str, profile_name: str) -> Dict[str, Any]:
    """Метрики предыдущего прогона той же пары. Пусто, если сравнивать не с чем."""
    try:
        history = run_history(results_root, operation, profile_name)
    except ResultStoreError:
        # Прогон без годной операции — чужой или дописанный руками файл. Отчёт собирается по
        # тому, что лежит на диске, и падать на одном таком файле он не должен.
        log.warning("у прогона нет годной операции (%r) — сравнивать не с чем", operation)
        return {}
    if len(history) < 2:
        return {}
    try:
        return json.loads(history[1].read_text(encoding="utf-8")).get("metrics", {})
    except (OSError, json.JSONDecodeError) as exc:
        log.warning("предыдущий прогон %s не прочитан: %s", history[1], exc)
        return {}


def degradation_by_size(raw_results: List[dict]) -> Dict[str, Dict[str, int]]:
    """Насколько просели ответы внутри каждого диапазона размера.

    Сравнивается первая треть замеров с последней — **во времени**, по `request_start_ts`.
    Сортировка по длительности давала бы разброс внутри диапазона, а не просадку: на
    ответах, которые вдвое ускорялись, и на тех, что вдвое замедлялись, получалось одно и
    то же число с одним и тем же знаком.

    Прототип считал это только для эндпоинтов, в чьём ключе есть слово «image». Признак
    берётся из данных: есть в результатах `size_range` — есть и разбивка. Имена эндпоинтов
    заказчика библиотеке не известны и известны быть не должны.
    """
    by_size: Dict[str, List[Tuple[float, float]]] = {}
    for r in raw_results:
        if r.get("success") and "duration_sec" in r and r.get("size_range"):
            by_size.setdefault(r["size_range"], []).append(
                (r.get("request_start_ts") or 0.0, r["duration_sec"]))

    out: Dict[str, Dict[str, int]] = {}
    for size, samples in by_size.items():
        if len(samples) < DEGRADATION_MIN_SAMPLE:
            continue
        in_time_order = [duration for _, duration in sorted(samples, key=lambda s: s[0])]
        edge = max(1, len(in_time_order) // 3)
        first_avg = mean(in_time_order[:edge])
        last_avg = mean(in_time_order[-edge:])
        out[size] = {
            "count": len(samples),
            "first_avg_ms": round(first_avg * 1000),
            "last_avg_ms": round(last_avg * 1000),
            "delta_ms": round((last_avg - first_avg) * 1000),
        }
    return out


def build_summary(runs: List[dict]) -> Optional[dict]:
    """Сводка по эндпоинту: как он в целом держит нагрузку."""
    valid = [r for r in runs if r["metrics"].get("success_rate") is not None]
    if not valid:
        return None

    # Стресс исключён из «самой стабильной нагрузки» намеренно: его задача — сломать
    # систему, и сравнивать его p95 с обычным профилем бессмысленно.
    non_stress = [r for r in valid if "stress" not in r["profile"].lower()]
    best = min(non_stress or valid, key=lambda r: r["metrics"].get("p95_ms", float("inf")))
    max_rps_run = max(valid, key=lambda r: r["metrics"].get("real_rps", 0))
    worst = max(valid, key=lambda r: r["metrics"].get("error_rate_pct", 0))

    saturation = max((r["metrics"]["saturation_point_concurrency"] for r in valid
                      if r["metrics"].get("saturation_point_concurrency")), default=None)

    if any(r["metrics"].get("error_rate_pct", 0) > BAD_ERROR_RATE_PCT for r in valid):
        status_class, status_text = "bad", "Критичные провалы под нагрузкой"
    elif any(r["metrics"].get("p95_ms", 0) > WARN_P95_MS for r in valid):
        status_class, status_text = "warn", "Высокие задержки"
    else:
        status_class, status_text = "good", "Стабильно держит"

    facts = []
    if saturation:
        facts.append(f"Выдерживает примерно до {saturation} одновременных запросов")
    if best["metrics"].get("p95_ms", 0) < GOOD_P95_MS:
        facts.append("В обычных режимах держит стабильно")
    if worst["metrics"].get("error_rate_pct", 0) > NOTABLE_ERROR_RATE_PCT:
        facts.append(f"Проблема в профиле {worst['profile_name']}: высокая доля ошибок")

    return {
        "status_class": status_class,
        "status_text": status_text,
        "best_profile": best["profile_name"],
        "best_p95": round(best["metrics"].get("p95_ms", 0)),
        "best_success": round(best["metrics"].get("success_rate", 0) * 100, 1),
        "max_rps": round(max_rps_run["metrics"].get("real_rps", 0), 1),
        "max_rps_profile": max_rps_run["profile_name"],
        "saturation": saturation,
        "worst_profile": worst["profile_name"],
        "worst_error": round(worst["metrics"].get("error_rate_pct", 0), 1),
        "worst_degradation": worst["metrics"].get("degradation_status", "—"),
        "recommendation_text": " • ".join(facts) or "Недостаточно данных",
    }


def _run_view(results_root: Path, profile_name: str, path: Path) -> Optional[dict]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        log.warning("прогон %s не прочитан: %s", path, exc)
        return None

    metrics = data.get("metrics", {})
    # Операция берётся из самого прогона: в имени каталога лежит её необратимый ключ, а в
    # файле — она сама. Поэтому отчёт показывает «GET /v1/items», а не хеш.
    operation = str(data.get("operation") or "")
    prev = previous_metrics(results_root, operation, profile_name)
    raw_results = data.get("raw_results") or []

    if raw_results:
        series = prepare_timeseries_data(raw_results, metrics.get("duration_sec", 300))
        plotly_config = create_plotly_config(
            series, title=f"{profile_name}: нагрузка во времени",
            has_concurrency="concurrency" in series)
        heatmap_config = create_error_heatmap_config(raw_results)
    else:
        plotly_config, heatmap_config = {}, {}

    profile_config = (data.get("config_snapshot") or {}).get("profile")
    if not profile_config:
        profile_config = {"warning": "Настройки профиля не сохранены вместе с прогоном"}

    return {
        "run_id": data.get("run_id", "—"),
        "operation": operation,
        "slug": str(data.get("slug") or ""),
        "profile": profile_name,
        "profile_name": profile_name.replace("_", " ").title(),
        "timestamp": data.get("started_at", "—"),
        "metrics": metrics,
        "prev_metrics": prev,
        "delta": {
            "rps": metrics.get("real_rps", 0) - prev.get("real_rps", 0),
            "p95": metrics.get("p95_ms", 0) - prev.get("p95_ms", 0),
            "error_rate": metrics.get("error_rate_pct", 0) - prev.get("error_rate_pct", 0),
        },
        "events": data.get("events", []),
        "has_raw": bool(raw_results),
        "plotly_json": json.dumps(plotly_config) if plotly_config else "{}",
        "error_heatmap_json": json.dumps(heatmap_config) if heatmap_config else "{}",
        "config_snapshot": data.get("config_snapshot", {}),
        "profile_config": profile_config,
        "degradation_by_size": degradation_by_size(raw_results),
    }


def collect_endpoint_groups(
    results_root: Union[str, Path],
    endpoint_names: Optional[Mapping[str, str]] = None,
) -> Dict[str, dict]:
    """Собирает данные дашборда: по операции — последние прогоны каждого профиля.

    Группировка идёт по операции, а не по подписи: два прогона одной операции, названные
    потребителем по-разному, — прогоны одного эндпоинта, и в отчёте они стоят рядом.
    Подпись остаётся заголовком группы, потому что читает отчёт человек.

    `endpoint_names` — необязательная замена заголовка, ключ в нём — операция.
    """
    root = Path(results_root)
    groups: Dict[str, dict] = {}
    for key, profile_name, path in iter_latest_runs(root):
        view = _run_view(root, profile_name, path)
        if view is None:
            continue
        operation = view["operation"]
        group = groups.setdefault(operation or key, {
            "name": (endpoint_names or {}).get(operation) or view["slug"] or operation or key,
            "operation": operation,
            "id": key,
            "runs": [],
            "summary": None,
        })
        group["runs"].append(view)

    for group in groups.values():
        group["summary"] = build_summary(group["runs"])
    return groups


def generate_dashboard(
    results_root: Union[str, Path],
    output_path: Union[str, Path],
    endpoint_names: Optional[Mapping[str, str]] = None,
    plotly_js: Union[str, Path, None] = None,
    now: Optional[str] = None,
) -> Optional[Path]:
    """Пишет HTML-дашборд. Возвращает путь к нему или `None`, если писать было не о чем."""
    groups = collect_endpoint_groups(results_root, endpoint_names)
    if not groups:
        log.warning("в %s нет сохранённых прогонов — дашборд не собран", results_root)
        return None

    bundle = plotly_bundle(plotly_js)
    html = _environment().get_template("dashboard.html.j2").render(
        endpoint_groups=groups,
        plotly_js=bundle,
        now=now or datetime.now().isoformat(),
    )

    out = Path(output_path)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(html, encoding="utf-8")
    log.info("дашборд собран: %s", out)
    return out
