"""Сборка дашборда и шаблоны отчёта.

Главное, что здесь сторожится, — красная линия про закрытый контур: открытый отчёт не
должен отправить ни одного запроса. Прототип тянул plotly и иконочный шрифт с CDN, и
в контуре заказчика такой отчёт открывается пустой страницей.

Запрещена именно **подгрузка** ресурса, а не всякое упоминание внешнего адреса: разницу
разбирает докстринг `test_the_report_asks_the_network_for_nothing`. Проверка держится на
том, что plotly.js вшит; в окружении без него она пуста, поэтому отсутствие бандла здесь —
падение, а не пропуск.
"""

from __future__ import annotations

import json
import re

import pytest

pytest.importorskip("jinja2", reason="отчёт живёт за extra report")
pytest.importorskip("pandas", reason="отчёт живёт за extra report")

from partest_load.reporting import generator  # noqa: E402
from partest_load.reporting.generator import (  # noqa: E402
    TEMPLATES_DIR,
    build_summary,
    collect_endpoint_groups,
    degradation_by_size,
    generate_dashboard,
    plotly_bundle,
)
from partest_load.storage.result_store import save_run_results  # noqa: E402
from partest_load.storage.schema import AggregatedMetrics  # noqa: E402

# Места, в которых браузер **идёт за ресурсом**. Отчёт — один файл: любая из этих записей
# означает, что при открытии он полезет наружу либо за соседний файл, которого рядом может
# не оказаться. Адрес здесь не важен — важна сама позиция.
FETCHES = (
    ("src=", re.compile(r"\bsrc\s*=", re.IGNORECASE)),
    ("<link>", re.compile(r"<link\b", re.IGNORECASE)),
    ("<iframe>", re.compile(r"<iframe\b", re.IGNORECASE)),
    ("@import", re.compile(r"@import\b", re.IGNORECASE)),
    ("@font-face", re.compile(r"@font-face\b", re.IGNORECASE)),
    ("url(...)", re.compile(r"\burl\(\s*[\"']?(?!data:)", re.IGNORECASE)),
    ("fetch()", re.compile(r"\bfetch\s*\(")),
    ("XMLHttpRequest", re.compile(r"\bXMLHttpRequest\b")),
    ("importScripts()", re.compile(r"\bimportScripts\s*\(")),
)

# Типы следов, которые plotly рисует, ничего не спрашивая у сети. Всё картографическое
# (`scattermap`, `choropleth`, `densitymap`, `scattergeo` и родня) тянет тайлы, стили и
# топологию с чужих адресов — см. докстринг `test_the_report_asks_the_network_for_nothing`.
LOCAL_TRACE_TYPES = {"bar", "scatter", "scattergl", "heatmap", "box", "histogram", "pie"}


def _metrics(**over) -> AggregatedMetrics:
    base = dict(total_requests=120, success_count=114, success_rate=0.95, error_rate_pct=5.0,
                real_rps=14.0, duration_sec=60.0, p50_ms=110.0, p95_ms=320.0, p99_ms=900.0,
                max_latency_ms=1500.0, mean_latency_ms=150.0, avg_response_mb=0.5,
                max_response_mb=2.0, errors_by_type={"server_5xx": 6})
    base.update(over)
    return AggregatedMetrics(**base)


def _raw(i: int, ok: bool = True, duration: float = 0.12, size_range: str = "1–3 MB") -> dict:
    return {
        "timestamp": "2026-09-12T10:00:00+00:00",
        "request_start_ts": 1_700_000_000.0 + i,
        "worker_id": "w1-aaaaaaaa",
        "phase": "steady",
        "url": "https://service.invalid/v1/items",
        "method": "POST",
        "status": 200 if ok else 503,
        "duration_sec": duration,
        "success": ok,
        "size_mb": 2.0,
        "size_range": size_range,
        "error_type": None if ok else "server_5xx",
        "active_concurrency": 10 + (i % 5),
    }


OPERATION = "POST /v1/items"


@pytest.fixture()
def results_root(tmp_path):
    raw = [_raw(i, ok=(i % 20 != 0), duration=0.1 + i * 0.002) for i in range(120)]
    save_run_results(
        tmp_path, operation=OPERATION, slug="items", profile_name="steady", metrics=_metrics(),
        events=[{"event": "phase_start"}],
        raw_results=raw, save_raw=True,
        config_snapshot={"profile": {"name": "Ровная нагрузка", "type": "linear",
                                    "duration_sec": 120, "target_concurrency": 30,
                                    "workers": 3, "stop_on_error_rate": 2.0}},
    )
    return tmp_path


def test_all_three_templates_are_present():
    """Шаблоны — данные, и теряются они тихо: код цел, отчёт не собирается."""
    names = {p.name for p in TEMPLATES_DIR.glob("*.j2")}

    assert names == {"dashboard.html.j2", "card.html.j2", "modal_full_report.html.j2"}


def test_plotly_is_embedded_and_not_linked(results_root, tmp_path):
    """Библиотека едет внутри файла, а не по адресу.

    Отчёт уносят из контура — в письмо, в тикет, на флешку — и открывают там, где сети
    нет. `<script src="...plotly...">` в таком файле означает пустую страницу.

    Здесь же закрыта дыра, из-за которой проверка автономности два месяца была пустой:
    в окружении без `plotly` библиотека в отчёт не вшивалась, искать в нём было нечего, и
    тест зеленел, ничего не проверив. Теперь отсутствие бандла — это падение с прямым
    указанием, чего не хватает в окружении, а не тихий зелёный.
    """
    bundle = plotly_bundle()
    assert bundle is not None, (
        "в этом окружении нет plotly, и проверять автономность отчёта не на чем: "
        "поставьте pip install -e \".[dev]\" — иначе проверка пуста, а не пройдена"
    )

    out = generate_dashboard(results_root, tmp_path / "out" / "dashboard.html")
    html = out.read_text(encoding="utf-8")

    assert bundle in html, "plotly.js не вшит в отчёт целиком"
    assert "Plotly.newPlot" in html
    assert re.search(r"<script[^>]*\bsrc\s*=", html, re.IGNORECASE) is None, (
        "в отчёте есть <script src=...> — код подгружается, а не лежит в файле"
    )
    assert "Графиков нет" not in html


def test_the_report_asks_the_network_for_nothing(results_root, tmp_path):
    """Открытый отчёт не отправляет ни одного запроса — ни наружу, ни за соседний файл.

    **Подгрузка и ссылка — разные вещи, и запрещена только первая.** Браузер идёт за
    ресурсом сам, без человека, в `src=`, `<link>`, `<iframe>`, `@import`, `@font-face`,
    `url(...)`, `fetch()`, `XMLHttpRequest`, `importScripts()`. Этого в отчёте нет нигде.
    `href` у `<a>` — приглашение кликнуть: пока по нему не щёлкнули, наружу не уходит
    ничего, и в закрытом контуре такой отчёт открывается полностью.

    Поэтому во вшитом `plotly.js` допустимы его собственные ссылки — логотип plotly в
    панели инструментов, атрибуция ESRI и OpenStreetMap в определениях карт. Предыдущая
    версия проверки искала `src=` и `href=` одним выражением, находила эти шесть ссылок и
    требовала бы выбросить кусок чужой библиотеки ради строки, которая ничего не грузит.

    Вшитый бандл вырезан из проверяемого текста намеренно: это чужой код целиком, и в его
    картографической части есть настоящие подгрузки (иконки с `cdn.jsdelivr.net`, стили
    подложек с `basemaps.cartocdn.com`, топология с `cdn.plot.ly`). Они спят: ни один
    график отчёта не картографический, и это отдельно сторожит
    `test_charts_use_only_traces_that_render_locally`. Всё, что вне бандла, — наше, и
    там запрещено любое обращение за ресурсом, хоть внешнее, хоть соседним файлом.
    """
    out = generate_dashboard(results_root, tmp_path / "out" / "dashboard.html")
    html = out.read_text(encoding="utf-8")

    bundle = plotly_bundle()
    assert bundle and bundle in html, "без вшитого бандла эта проверка снова пуста"
    ours = html.replace(bundle, "")

    found = {name: rx.search(ours).group(0) for name, rx in FETCHES if rx.search(ours)}

    assert not found, f"отчёт идёт за ресурсом: {found}"
    assert "cdn." not in ours
    assert "cdnjs" not in ours
    assert "fonts.googleapis" not in ours


def test_charts_use_only_traces_that_render_locally(results_root):
    """Ни один график не картографический.

    Во вшитом plotly.js лежит код карт, а он адреса подложек и иконок действительно
    запрашивает. Единственное, что держит эти строки мёртвыми, — то, что отчёт таких
    следов не строит. Появится картографический график — отчёт начнёт ходить в сеть, и
    заметить это по тексту файла уже не получится.
    """
    groups = collect_endpoint_groups(results_root)
    figures = [json.loads(run[key])
               for group in groups.values() for run in group["runs"]
               for key in ("plotly_json", "error_heatmap_json")]

    types = {trace.get("type") for figure in figures for trace in figure.get("data", [])}

    assert types, "в прогоне не оказалось ни одного графика — проверять нечего"
    assert types <= LOCAL_TRACE_TYPES, f"следы, которым нужна сеть: {types - LOCAL_TRACE_TYPES}"


def test_dashboard_says_so_when_charts_are_missing(results_root, tmp_path, monkeypatch):
    """Без plotly.js отчёт собирается с таблицами и честно пишет, почему нет графиков.

    Подставить ссылку на CDN вместо предупреждения — самое естественное «исправление»
    этого места, и именно оно запрещено.

    Отсутствие бандла подставляется, а не берётся из окружения: `plotly_js=None` значит
    «возьми из установленного пакета», и там, где `plotly` стоит, этот тест пропускался —
    то есть проверял свою ветку только в окружении, в котором отчёт всё равно неполон.
    """
    monkeypatch.setattr(generator, "plotly_bundle", lambda *_a, **_kw: None)

    out = generate_dashboard(results_root, tmp_path / "dashboard.html")
    html = out.read_text(encoding="utf-8")

    assert "Графиков нет" in html
    assert "Plotly.newPlot" not in html
    assert re.search(r"<script[^>]*\bsrc\s*=", html, re.IGNORECASE) is None


def test_embedded_plotly_is_not_escaped(results_root, tmp_path):
    """Вшитая библиотека попадает в файл как код, а не как текст.

    Автоэкранирование Jinja превратило бы каждую `<` в `&lt;` — отчёт открылся бы, графиков
    в нём не было бы, и ни одна проверка «файл собрался» этого не заметила.
    """
    bundle = tmp_path / "plotly.js"
    bundle.write_text("window.Plotly = {newPlot: function (a, b) { return a < b; }};",
                      encoding="utf-8")

    out = generate_dashboard(results_root, tmp_path / "dashboard.html", plotly_js=bundle)
    html = out.read_text(encoding="utf-8")

    assert "return a < b;" in html
    assert "&lt;" not in html.split("</script>")[0]


def test_dashboard_shows_the_profile_the_run_went_with(results_root, tmp_path):
    """Настройки берутся из самого прогона, а не из сегодняшнего файла профиля."""
    out = generate_dashboard(results_root, tmp_path / "dashboard.html")
    html = out.read_text(encoding="utf-8")

    assert "Ровная нагрузка" in html
    assert "Настройки профиля" in html
    assert "Настройки профиля не сохранены" not in html


def test_size_breakdown_reaches_the_page(results_root, tmp_path):
    """Разбивка по размеру ответа рендерится.

    В прототипе шаблон проверял имя, которого в контексте не было ни разу, — таблица не
    появлялась никогда, хотя считалась.
    """
    out = generate_dashboard(results_root, tmp_path / "dashboard.html")
    html = out.read_text(encoding="utf-8")

    assert "диапазона размеров" in html
    assert "1–3 MB" in html


def test_empty_results_root_yields_no_dashboard(tmp_path):
    assert generate_dashboard(tmp_path / "nothing", tmp_path / "dashboard.html") is None


def test_endpoint_name_is_optional_and_never_taken_from_code(results_root, tmp_path):
    """Человеческие названия приходят снаружи; без них берётся подпись из прогона."""
    plain = generate_dashboard(results_root, tmp_path / "a.html").read_text(encoding="utf-8")
    assert "items" in plain

    named = generate_dashboard(results_root, tmp_path / "b.html",
                               endpoint_names={OPERATION: "Каталог позиций"})
    assert "Каталог позиций" in named.read_text(encoding="utf-8")


def test_the_dashboard_shows_the_operation_not_the_folder_key(results_root, tmp_path):
    """Операция видна в отчёте в исходном виде.

    В имени каталога лежит её необратимый ключ; показать его человеку — значит показать хеш
    вместо «POST /v1/items». Подпись при этом остаётся заголовком: её читает человек.
    """
    html = generate_dashboard(results_root, tmp_path / "dashboard.html").read_text(
        encoding="utf-8")

    assert OPERATION in html


def test_runs_of_one_operation_stand_together_whatever_they_are_called(tmp_path):
    """Группировка — по операции, а не по подписи.

    Переименование эндпоинта у потребителя не должно разваливать его историю на два
    эндпоинта в отчёте.
    """
    save_run_results(tmp_path, operation=OPERATION, slug="items", profile_name="steady",
                     metrics=_metrics(), events=[])
    save_run_results(tmp_path, operation=OPERATION, slug="orders-create", profile_name="stress",
                     metrics=_metrics(), events=[])

    groups = collect_endpoint_groups(tmp_path)

    assert list(groups) == [OPERATION]
    assert len(groups[OPERATION]["runs"]) == 2


def test_two_operations_with_one_signature_stay_two_endpoints(tmp_path):
    """Одинаковая подпись не склеивает разные операции в одну карточку."""
    save_run_results(tmp_path, operation=OPERATION, slug="items", profile_name="steady",
                     metrics=_metrics(), events=[])
    save_run_results(tmp_path, operation="GET /v1/items/{id}", slug="items",
                     profile_name="steady", metrics=_metrics(), events=[])

    groups = collect_endpoint_groups(tmp_path)

    assert set(groups) == {OPERATION, "GET /v1/items/{id}"}
    assert all(len(group["runs"]) == 1 for group in groups.values())


def test_degradation_by_size_needs_a_size_range_in_the_data():
    """Признак берётся из данных, а не из имени эндпоинта.

    Прототип считал разбивку только там, где в ключе эндпоинта встречалось слово «image»:
    домен заказчика, зашитый в библиотеку.
    """
    without = [dict(_raw(i), size_range=None) for i in range(30)]
    assert degradation_by_size(without) == {}

    with_range = [_raw(i, duration=0.1 + i * 0.01) for i in range(30)]
    stats = degradation_by_size(with_range)
    assert set(stats) == {"1–3 MB"}
    assert stats["1–3 MB"]["count"] == 30


def test_degradation_by_size_tells_a_slowdown_from_a_speedup():
    """Просадка считается по времени прогона, а не по разбросу длительностей.

    Сортировка замеров по длительности даёт одно и то же положительное число и на
    замедлении, и на ускорении: «насколько просели ответы» превращается в «насколько они
    вообще разные». Отличить одно от другого — единственное, ради чего эта таблица в отчёте.
    """
    slowing = [_raw(i, duration=0.1 + i * 0.01) for i in range(12)]
    speeding = [_raw(i, duration=0.21 - i * 0.01) for i in range(12)]

    slow_delta = degradation_by_size(slowing)["1–3 MB"]["delta_ms"]
    fast_delta = degradation_by_size(speeding)["1–3 MB"]["delta_ms"]

    assert slow_delta > 0
    assert fast_delta < 0
    assert slow_delta == -fast_delta


def test_degradation_by_size_does_not_trust_the_order_of_the_list():
    """Порядок списка — «фаза → воркер → время», и полагаться на него нельзя."""
    in_time_order = [_raw(i, duration=0.1 + i * 0.01) for i in range(12)]
    shuffled = in_time_order[7:] + in_time_order[:7]

    assert degradation_by_size(shuffled) == degradation_by_size(in_time_order)


def test_summary_ignores_stress_when_naming_the_steadiest_profile():
    """Задача stress — сломать систему, и его p95 нельзя сравнивать с обычным профилем."""
    runs = [
        {"profile": "stress_hard", "profile_name": "Stress Hard",
         "metrics": {"success_rate": 0.5, "p95_ms": 80.0, "real_rps": 200.0,
                     "error_rate_pct": 40.0}},
        {"profile": "steady", "profile_name": "Steady",
         "metrics": {"success_rate": 0.99, "p95_ms": 300.0, "real_rps": 20.0,
                     "error_rate_pct": 0.5}},
    ]

    summary = build_summary(runs)

    assert summary["best_profile"] == "Steady"
    assert summary["worst_profile"] == "Stress Hard"
    assert summary["status_class"] == "bad"


def test_summary_needs_something_to_summarise():
    assert build_summary([{"profile": "x", "profile_name": "X", "metrics": {}}]) is None


def test_run_without_raw_results_still_gets_a_card(tmp_path):
    save_run_results(tmp_path, operation=OPERATION, slug="items", profile_name="quiet",
                     metrics=_metrics(), events=[])

    out = generate_dashboard(tmp_path, tmp_path / "dashboard.html")
    html = out.read_text(encoding="utf-8")

    assert "Quiet" in html
    assert "Настройки профиля не сохранены" in html
    assert json.dumps({}) == "{}"  # в карточке фигуры пусты, и шаблон это переживает
