"""Учётные данные базы, журнал, ошибки разбора, вывод плана, сводки и дашборда.

Сети не нужно: где запрос должен действительно пройти через httpx (журнал, авторизация),
клиент получает `httpx.MockTransport`.
"""

from __future__ import annotations

import asyncio
import logging

import httpx
import pytest

from partest_load import cli
from partest_load.configs.endpoints import EndpointConfig
from partest_load.configs.profiles import ProfileError, parse_profile
from partest_load.core import client as client_module
from partest_load.core.client import make_single_request
from partest_load.engines.stress import StressEngine

PW_CANARY = "canary-pw-not-real"
QUERY_CANARY = "canary-query-not-real"
HEADER_CANARY = "canary-header-not-real"
ENDPOINT_YAML = ("items:\n  name: x\n  base_url: https://user:" + PW_CANARY + "@service.invalid\n"
                 "  path: /x\n  method: GET\n  query_params: {marker: " + QUERY_CANARY + "}\n")


def _files(tmp_path, endpoints_yaml: str, profile_yaml: str):
    endpoints = tmp_path / "endpoints.yaml"
    endpoints.write_text(endpoints_yaml, encoding="utf-8")
    profile = tmp_path / "profile.yaml"
    profile.write_text(profile_yaml, encoding="utf-8")
    return ["--endpoints", str(endpoints), "--endpoint", "items", "--profile", str(profile),
            "--results-root", str(tmp_path / "out")]


# ── журнал httpx и учётные данные базы ────────────────────────────────────────────

def test_an_info_run_logs_neither_the_password_nor_query_values(tmp_path, monkeypatch,
                                                                 caplog, capsys):
    seen = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json={})

    real_client = httpx.AsyncClient
    monkeypatch.setattr(client_module.httpx, "AsyncClient",
                        lambda **kw: real_client(transport=httpx.MockTransport(handler),
                                                 **{**kw, "http2": False}))
    argv = _files(tmp_path, ENDPOINT_YAML,
                  "type: linear\nduration_sec: 0.5\nworkers: 1\nrate_limit_rps: 20\n"
                  "ramp_up_sec: 0\n")
    caplog.set_level(logging.INFO)
    try:
        assert cli.main(argv + ["--log-level", "INFO"]) == 0
    finally:
        for name in ("httpx", "httpcore", "h2", "hpack"):
            logging.getLogger(name).setLevel(logging.NOTSET)

    out = capsys.readouterr()
    everything = caplog.text + out.out + out.err
    assert seen, "прогон не отправил ни одного запроса — проверка пустая"
    assert PW_CANARY not in everything
    assert QUERY_CANARY not in everything
    # Учётные данные дошли до стенда — авторизацией, а не адресом.
    assert seen[0].headers["Authorization"].startswith("Basic ")
    assert "@" not in str(seen[0].url)


def test_userinfo_is_not_part_of_the_address(tmp_path, capsys):
    endpoint = EndpointConfig(name="x", base_url=f"https://user:{PW_CANARY}@service.invalid",
                              path="/x", method="GET")
    assert endpoint.basic_auth() == ("user", PW_CANARY)
    assert PW_CANARY not in endpoint.url() and PW_CANARY not in repr(endpoint)

    async def go():
        transport = httpx.MockTransport(lambda request: httpx.Response(200))
        async with httpx.AsyncClient(transport=transport) as client:
            return await make_single_request(client, endpoint)

    assert PW_CANARY not in asyncio.run(go())["url"]

    argv = _files(tmp_path, ENDPOINT_YAML, "type: linear\nduration_sec: 60\nworkers: 1\n")
    assert cli.main(argv + ["--dry-run"]) == 0
    assert PW_CANARY not in capsys.readouterr().out


# ── ошибки профиля без значений ─────────────────────────────────────────────────

def test_a_profile_error_does_not_quote_values(tmp_path, capsys):
    with pytest.raises(ProfileError) as info:
        parse_profile({"type": "linear", "workers": "sentinel-canary", "name": ["sentinel-two"]})
    assert "sentinel" not in str(info.value)
    assert "workers" in str(info.value)
    assert info.value.__cause__ is None

    argv = _files(tmp_path, "items:\n  name: x\n  base_url: https://service.invalid\n"
                            "  path: /x\n  method: GET\n",
                  "type: ramp\nphases:\n  - {duration_sec: sentinel-canary, target_concurrency: 1}\n")
    assert cli.main(argv + ["--dry-run"]) == 1
    assert "sentinel-canary" not in capsys.readouterr().err


# ── база без query и fragment ────────────────────────────────────────────────────

@pytest.mark.parametrize("base", ["https://service.invalid/api?token=x", "https://service.invalid/#frag"])
def test_a_base_url_with_query_or_fragment_is_refused(base):
    with pytest.raises(ValueError, match="query_params"):
        EndpointConfig(name="x", base_url=base, path="/x", method="GET")


# ── JSON графиков внутри <script> ────────────────────────────────────────────────

def test_a_script_tag_in_run_data_does_not_close_the_chart_block(tmp_path):
    pytest.importorskip("jinja2")
    pytest.importorskip("pandas")
    from partest_load.reporting.generator import generate_dashboard
    from partest_load.storage.result_store import save_run_results
    from partest_load.storage.schema import AggregatedMetrics

    evil = "</script><script>alert(1)</script>"
    raw = [{"timestamp": "2026-10-17T10:00:00+00:00", "request_start_ts": 1_700_000_000.0 + i,
            "worker_id": "w1", "phase": "steady", "url": "https://service.invalid/x",
            "method": "GET", "status": 500, "duration_sec": 0.1, "success": i % 2 == 0,
            "size_mb": 0.1, "error_type": None if i % 2 == 0 else evil,
            "active_concurrency": 1} for i in range(60)]
    metrics = AggregatedMetrics(total_requests=60, success_count=30, success_rate=0.5,
                                error_rate_pct=50.0, real_rps=1.0, duration_sec=60.0, p50_ms=100,
                                p95_ms=100, p99_ms=100, max_latency_ms=100, mean_latency_ms=100,
                                avg_response_mb=0.1, max_response_mb=0.1)
    save_run_results(tmp_path, operation="GET /x", slug="x", profile_name="steady",
                     metrics=metrics, events=[], raw_results=raw, save_raw=True)
    html = generate_dashboard(tmp_path, tmp_path / "d.html").read_text(encoding="utf-8")

    assert evil not in html
    assert "alert(1)" in html  # данные на месте, только закодированы


# ── план stress ─────────────────────────────────────────────────────────────

def test_the_stress_plan_shows_the_hold_and_notes_once(tmp_path, capsys):
    profile = parse_profile({"type": "stress", "initial_concurrency": 10, "max_concurrency": 20,
                             "ramp_up_step": 10, "hold_after_breaking_sec": 45,
                             "stop_on_error_rate": 0})
    engine = StressEngine(profile, None)
    lines = cli._describe_plan(engine)
    text = "\n".join(lines)
    assert "hold-after-break" in text and "+45 с" in text
    assert any("stop_on_error_rate" in note for note in engine.plan_notes())  # S3
    assert "внимание" in text
    assert "внимание" not in "\n".join(cli._describe_plan(engine, with_notes=False))


# ── ошибка уровня модели с заголовком ──────────────────────────────────

def test_a_model_level_error_does_not_print_header_values(tmp_path, capsys):
    # Заголовки первыми и коротко: без защиты pydantic цитирует вход с начала и обрезает
    # хвост, и только так тест отличает защиту от обрезки.
    argv = _files(tmp_path, "items:\n  headers: {A: S3CR}\n  name: x\n"
                            "  base_url: https://service.invalid\n"
                            "  path: /v1/items/{id}\n  method: GET\n"
                            "  path_params: {item_id: 1}\n",
                  "type: linear\nduration_sec: 60\nworkers: 1\n")
    assert cli.main(argv + ["--dry-run"]) == 1
    out = capsys.readouterr()
    assert "item_id" in out.err
    assert "S3CR" not in out.err + out.out


# ── сводка, адрес в выводе, удержание после отказа ────────────────────────────────────────────────────────────

def test_the_cli_summary_shows_the_stop_event(tmp_path, monkeypatch, capsys):
    from partest_load.core.metrics import RunMetrics

    async def fake_run(endpoint_cfg, profile):
        return [], RunMetrics(), [{"event": "stopped", "phase": "steady", "concurrency": 5,
                                   "reasons": ["доля ошибок 50.0%"]}]

    monkeypatch.setattr(cli, "run_load_test", fake_run)
    argv = _files(tmp_path, "items:\n  name: x\n  base_url: https://service.invalid\n"
                            "  path: /x\n  method: GET\n",
                  "type: linear\nduration_sec: 60\nworkers: 1\n")
    assert cli.main(argv) == 0
    assert "ОСТАНОВЛЕН" in capsys.readouterr().out


def test_hold_after_break_runs_at_the_level_tempo():
    profile = parse_profile({"type": "stress", "workers": 2, "rate_limit_rps": 10,
                             "initial_concurrency": 10, "max_concurrency": 50,
                             "ramp_up_step": 10, "ramp_up_interval_sec": 5,
                             "hold_after_breaking_sec": 30})
    engine = StressEngine(profile, None)
    phases = []

    async def failing(phase, stop_check=None):
        phases.append(phase)
        return [{"request_start_ts": float(i), "duration_sec": 0.01, "success": False}
                for i in range(200)]

    engine._run_phase = failing

    async def drain():
        return [item async for item in engine.generate_phases()]

    asyncio.run(drain())
    assert [p.name for p in phases] == ["stress-lvl-10", "hold-after-break"]
    assert phases[1].rate_limit_rps == phases[0].rate_limit_rps
    assert phases[1].max_requests_per_worker == phases[0].max_requests_per_worker
    assert engine.events[0]["event"] == "stopped"
