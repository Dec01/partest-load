"""Точка входа.

Объявленная в `setup.py` команда, которая ни во что не ведёт, хуже отсутствующей:
установка пакета даёт рабочий на вид `partest-load`, падающий при первом запуске. Поэтому
первым тестом проверяется сама запись `console_scripts`, а не её содержимое.

Второе, что сторожится здесь, — «нагрузка только по явной команде»: ни импорт, ни разбор
конфигурации, ни `--dry-run` не отправляют ни одного запроса. Тесты обходятся без сети
именно потому, что инструмент её не трогает до явного указания.
"""

from __future__ import annotations

import importlib
import re
from pathlib import Path

import pytest

from partest_load.cli import build_parser, main, print_summary
from partest_load.storage.schema import AggregatedMetrics

SETUP = "setup.py"
ENDPOINTS_YAML = """
items:
  name: Каталог позиций
  base_url: https://service.invalid
  path: /v1/items
  method: POST
  timeout: 15
  payloads:
    - body: {query: "пример"}
      weight: 1.0
"""
PROFILE_YAML = """
name: Ровная нагрузка
type: linear
duration_sec: 60
target_concurrency: 10
workers: 2
ramp_up_sec: 0
ramp_down_sec: 0
"""


def test_declared_console_script_resolves(repo_root):
    """Запись `console_scripts` ведёт в существующую функцию."""
    text = (repo_root / SETUP).read_text(encoding="utf-8")
    entry = re.search(r"\"partest-load=([^\"]+)\"", text)

    assert entry, "в setup.py нет точки входа partest-load"
    module_name, _, attr = entry.group(1).partition(":")
    module = importlib.import_module(module_name)

    assert callable(getattr(module, attr))


def test_report_templates_are_declared_as_package_data(repo_root):
    """Шаблоны отчёта объявлены данными пакета.

    Без этой строки колесо собирается без `reporting/templates`, и отчёт падает у
    потребителя — после прогона, то есть в самый дорогой момент.
    """
    text = (repo_root / SETUP).read_text(encoding="utf-8")

    assert "package_data={" in text
    assert re.search(r"\"partest_load\"\s*:\s*\[[^\]]*reporting/templates/\*\.j2", text)


@pytest.fixture()
def repo_root() -> Path:
    return Path(__file__).resolve().parent.parent


@pytest.fixture()
def config_files(tmp_path):
    endpoints = tmp_path / "endpoints.yaml"
    endpoints.write_text(ENDPOINTS_YAML, encoding="utf-8")
    profile = tmp_path / "steady.yaml"
    profile.write_text(PROFILE_YAML, encoding="utf-8")
    return endpoints, profile


def test_dry_run_sends_nothing(tmp_path, config_files, capsys):
    """Сухой прогон разбирает конфигурацию и останавливается.

    Если когда-нибудь `--dry-run` начнёт доходить до `run_load_test`, тест упадёт не по
    сравнению строк, а по попытке сходить на несуществующий адрес.
    """
    endpoints, profile = config_files
    results = tmp_path / "results"

    code = main(["--endpoints", str(endpoints), "--endpoint", "items",
                 "--profile", str(profile), "--results-root", str(results), "--dry-run"])

    assert code == 0
    assert not results.exists()
    out = capsys.readouterr().out
    assert "ни один запрос не отправлен" in out
    assert "https://service.invalid/v1/items" in out
    # Операция видна ещё до нагрузки: именно ею прогон опознаётся в раскладке и снаружи, и
    # проверять её задним числом, по имени каталога с хешем, вдвое дороже.
    assert "операция прогона: POST /v1/items" in out


def test_unknown_endpoint_key_lists_what_there_is(tmp_path, config_files, capsys):
    endpoints, profile = config_files

    code = main(["--endpoints", str(endpoints), "--endpoint", "missing",
                 "--profile", str(profile), "--dry-run"])

    assert code == 1
    assert "items" in capsys.readouterr().err


def test_broken_profile_is_reported_without_a_traceback(tmp_path, config_files, capsys):
    endpoints, _ = config_files
    bad = tmp_path / "bad.yaml"
    bad.write_text("type: linear\nramp_step: 50\n", encoding="utf-8")

    code = main(["--endpoints", str(endpoints), "--endpoint", "items",
                 "--profile", str(bad), "--dry-run"])

    assert code == 1
    assert "ProfileError" in capsys.readouterr().err


def test_missing_file_is_reported_not_raised(tmp_path, config_files, capsys):
    endpoints, profile = config_files

    code = main(["--endpoints", str(tmp_path / "nope.yaml"), "--endpoint", "items",
                 "--profile", str(profile), "--dry-run"])

    assert code == 1


def test_run_arguments_are_not_required_for_a_report(tmp_path, capsys):
    """`--report-only` не требует эндпоинта и профиля.

    В прототипе они были объявлены обязательными всегда, и режим «только отчёт» нельзя было
    вызвать, не назвав то, что в нём не используется.
    """
    code = main(["--report-only", "--results-root", str(tmp_path / "empty")])

    assert code == 1  # прогонов нет, но до проверки аргументов дело дошло
    assert "нет сохранённых прогонов" in capsys.readouterr().err


def test_run_without_configuration_is_refused_by_the_parser():
    with pytest.raises(SystemExit):
        main([])


def test_parser_knows_the_documented_switches():
    options = {action.option_strings[0] for action in build_parser()._actions
               if action.option_strings}

    assert {"--endpoints", "--endpoint", "--profile", "--results-root",
            "--save-raw", "--dry-run", "--report", "--report-only"} <= options


def test_summary_shows_what_a_load_run_is_read_for(capsys):
    metrics = AggregatedMetrics(
        total_requests=1000, success_count=900, success_rate=0.9, error_rate_pct=10.0,
        real_rps=42.0, duration_sec=120.0, p50_ms=100.0, p95_ms=800.0, p99_ms=2500.0,
        max_latency_ms=4000.0, mean_latency_ms=180.0, avg_response_mb=0.3,
        max_response_mb=1.0, errors_by_type={"timeout": 80, "server_5xx": 20},
        saturation_point_concurrency=240, degradation_slope_pct_per_min=18.2,
        degradation_status="moderate")

    print_summary(metrics)
    out = capsys.readouterr().out

    assert "42.0" in out
    assert "240" in out
    assert "timeout" in out and "80" in out
    assert "moderate" in out
