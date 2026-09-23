"""Данные графиков дашборда.

Модуль нужен только отчёту и живёт за extra `report`: `pandas` и `numpy` весят больше,
чем всё остальное вместе, а тому, кто снимает артефакт для карты, они не нужны.
`plotly` импортируется ещё позже — только там, где без него не обойтись.

Арифметика бинов перенесена из прототипа как есть, включая её странности: см. комментарий
у `bin_seconds`. Отчёт сравнивают с прошлым отчётом, и «починенная» ширина бина сдвинула бы
все кривые разом.
"""

from __future__ import annotations

import logging
from typing import Any, Dict, List

import numpy as np
import pandas as pd

log = logging.getLogger(__name__)

EMPTY_SERIES: Dict[str, Any] = {
    "times": [],
    "rps": [],
    "mean_latency": [],
    "p50": [],
    "p95": [],
    "p99": [],
    "success_rate": [],
    "error_rate": [],
    "concurrency": [],
}


def prepare_timeseries_data(
    results: List[Dict[str, Any]],
    duration_sec: float = 300.0,
    min_bins: int = 20,
    max_bins: int = 80,
) -> Dict[str, Any]:
    """Раскладывает сырые запросы по временным бинам: RPS, перцентили, ошибки, параллельность."""
    if not results:
        return dict(EMPTY_SERIES)

    df = pd.DataFrame(results)
    if "request_start_ts" not in df.columns:
        return dict(EMPTY_SERIES)

    if "error_type" not in df.columns:
        df["error_type"] = None
    df["error_type"] = df.apply(
        lambda row: "success" if row.get("success", True) else (row["error_type"] or "unknown"),
        axis=1,
    )

    df["ts"] = pd.to_datetime(df["request_start_ts"], unit="s")
    df = df.set_index("ts")

    # Ширина бина. Формула прототипа: сначала «не длиннее 30 с и не грубее min_bins»,
    # затем ограничение по max_bins, которое почти всегда и решает. Оставлена как есть.
    bin_seconds = max(1, min(30, int(duration_sec / min_bins)))
    bin_seconds = min(bin_seconds, max(1, int(duration_sec / max_bins)))

    def _pct(q: float):
        def inner(values):
            return np.percentile(values, q) if len(values) > 0 else np.nan
        inner.__name__ = f"p{int(q)}"
        return inner

    agg_dict: Dict[str, Any] = {
        "duration_sec": ["count", "mean", _pct(50), _pct(95), _pct(99)],
        "success": "mean",
        "size_mb": "mean",
    }
    has_errors = bool((df["error_type"] != "success").any())
    if has_errors:
        agg_dict["error_type"] = lambda x: (x != "success").sum()

    binned = df.resample(f"{bin_seconds}s").agg(agg_dict)

    base_columns = ["count", "mean_latency", "p50", "p95", "p99", "success_rate", "avg_size_mb"]
    if has_errors:
        binned.columns = base_columns + ["error_count"]
    else:
        binned.columns = base_columns
        binned["error_count"] = 0

    binned["rps"] = binned["count"] / bin_seconds
    binned["error_rate"] = binned["error_count"] / binned["count"].replace(0, 1) * 100

    binned = binned.fillna({
        "rps": 0,
        "error_rate": 0,
        "error_count": 0,
        "avg_size_mb": 0,
        "mean_latency": 0,
        "p50": 0,
        "p95": 0,
        "p99": 0,
        # Пустой интервал — это не провал успешности, а отсутствие запросов.
        "success_rate": 1.0,
    })
    binned = binned.interpolate(method="linear", limit_direction="both").fillna(0)

    if "active_concurrency" in df.columns:
        concurrency = df["active_concurrency"].resample(f"{bin_seconds}s").max()
        binned["concurrency"] = concurrency.ffill().bfill().fillna(0)
    else:
        binned["concurrency"] = 0

    start_ts = df.index.min()
    times = ([] if pd.isna(start_ts)
             else (binned.index - start_ts).total_seconds().round(0).astype(int).tolist())

    return {
        "times": times,
        "rps": binned["rps"].round(1).tolist(),
        "mean_latency": (binned["mean_latency"] * 1000).round(1).tolist(),
        "p50": (binned["p50"] * 1000).round(1).tolist(),
        "p95": (binned["p95"] * 1000).round(1).tolist(),
        "p99": (binned["p99"] * 1000).round(1).tolist(),
        "success_rate": (binned["success_rate"] * 100).round(1).tolist(),
        "error_rate": binned["error_rate"].round(1).tolist(),
        "concurrency": binned["concurrency"].round(0).astype(int).tolist(),
    }


def create_plotly_config(
    data: Dict[str, Any],
    title: str = "Нагрузка во времени",
    has_concurrency: bool = True,
) -> Dict[str, Any]:
    """Описание графика для Plotly. Это словарь, а не объект: сам plotly здесь не нужен."""
    if not data.get("times"):
        return {"data": [], "layout": {}, "config": {}}

    traces = [
        {
            "x": data["times"], "y": data["rps"], "type": "bar", "name": "RPS",
            "marker": {"color": "#1f77b4", "opacity": 0.7}, "yaxis": "y1",
        },
        {
            "x": data["times"], "y": data["p50"], "type": "scatter", "mode": "lines",
            "name": "p50, мс", "line": {"color": "#ff7f0e", "width": 2}, "yaxis": "y2",
        },
        {
            "x": data["times"], "y": data["p95"], "type": "scatter", "mode": "lines",
            "name": "p95, мс", "line": {"color": "#d62728", "width": 2}, "yaxis": "y2",
        },
        {
            "x": data["times"], "y": data["p99"], "type": "scatter", "mode": "lines",
            "name": "p99, мс", "line": {"color": "#9467bd", "width": 2, "dash": "dot"},
            "yaxis": "y2",
        },
        {
            "x": data["times"], "y": data["error_rate"], "type": "scatter",
            "mode": "lines+markers", "name": "Доля ошибок, %",
            "line": {"color": "#e377c2", "width": 2}, "marker": {"size": 6}, "yaxis": "y3",
        },
    ]

    if has_concurrency and any(data.get("concurrency", [])):
        traces.append({
            "x": data["times"], "y": data["concurrency"], "type": "scatter", "mode": "lines",
            "name": "Параллельность", "line": {"color": "#17becf", "width": 2, "dash": "dash"},
            "yaxis": "y4",
        })

    layout = {
        "title": {"text": title, "font": {"size": 18}},
        "height": 650,
        "xaxis": {"title": "Секунды от начала", "tickformat": ".0f"},
        "yaxis": {"title": "RPS", "titlefont": {"color": "#1f77b4"},
                  "tickfont": {"color": "#1f77b4"}, "side": "left"},
        "yaxis2": {"title": "Задержка, мс", "titlefont": {"color": "#d62728"},
                   "tickfont": {"color": "#d62728"}, "overlaying": "y", "side": "right"},
        "yaxis3": {"title": "Доля ошибок, %", "titlefont": {"color": "#e377c2"},
                   "tickfont": {"color": "#e377c2"}, "overlaying": "y", "side": "right",
                   "position": 0.95, "range": [0, 100]},
        "yaxis4": {"title": "Параллельность", "titlefont": {"color": "#17becf"},
                   "tickfont": {"color": "#17becf"}, "overlaying": "y", "side": "left",
                   "position": 0.05},
        "legend": {"orientation": "h", "yanchor": "bottom", "y": 1.02,
                   "xanchor": "center", "x": 0.5},
        "margin": {"t": 80, "b": 60, "l": 80, "r": 80},
        "hovermode": "x unified",
        "template": "plotly_white",
    }

    return {
        "data": traces,
        "layout": layout,
        "config": {"responsive": True, "displayModeBar": False, "staticPlot": False},
    }


def error_matrix(
    results: List[Dict[str, Any]],
    bin_seconds: int = 30,
) -> Dict[str, Any]:
    """Ошибки, разложенные по типу и времени: подписи осей и матрица счётчиков.

    Отдельно от построения картинки: считать нечего, если ошибок нет, и это видно
    без plotly.
    """
    empty: Dict[str, Any] = {"x": [], "y": [], "z": []}
    if not results:
        return empty

    failed = [r for r in results if not r.get("success", True)]
    if not failed:
        return empty

    df = pd.DataFrame(failed)
    if "request_start_ts" not in df.columns:
        return empty
    if "error_type" not in df.columns:
        df["error_type"] = "unknown"
    df["error_type"] = df["error_type"].fillna("unknown")

    df["ts_bin"] = pd.to_datetime(df["request_start_ts"], unit="s").dt.floor(f"{bin_seconds}s")
    pivot = pd.pivot_table(df, values="request_start_ts", index="error_type",
                           columns="ts_bin", aggfunc="count", fill_value=0)
    if pivot.empty or len(pivot.columns) == 0:
        return empty

    start = df["ts_bin"].min()
    labels = [f"{int((t - start).total_seconds() / 60)} мин" for t in pivot.columns]
    return {"x": labels, "y": pivot.index.tolist(), "z": pivot.values.tolist()}


def create_error_heatmap_config(
    results: List[Dict[str, Any]],
    title: str = "Ошибки по типу и времени",
    bin_seconds: int = 30,
) -> Dict[str, Any]:
    """Тепловая карта ошибок в виде описания фигуры Plotly."""
    matrix = error_matrix(results, bin_seconds=bin_seconds)
    if not matrix["z"]:
        return {"data": [], "layout": {}}

    return {
        "data": [{
            "type": "heatmap",
            "x": matrix["x"],
            "y": matrix["y"],
            "z": matrix["z"],
            "colorscale": "Reds",
            "colorbar": {"title": "Ошибок"},
            # Прототип брал `px.imshow(text_auto=True)`: число видно прямо в клетке.
            "texttemplate": "%{z}",
            "hovertemplate": "%{y}<br>%{x}: %{z}<extra></extra>",
        }],
        "layout": {
            "title": {"text": title, "font": {"size": 16}},
            "height": 450,
            "xaxis": {"title": "Минуты от начала"},
            "yaxis": {"title": "Тип ошибки"},
            "margin": {"l": 120, "r": 50, "t": 50, "b": 50},
        },
    }
