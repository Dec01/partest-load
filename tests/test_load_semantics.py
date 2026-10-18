"""Что делают профили и описание эндпоинта: темп stress, список batch, путь, тело, пороги.

Каждое поведение здесь заявлено в документации или в схеме конфигурации; тест держит
заявленное и то, что недозаявленное (адрес из тела запроса) больше не действует. Сети ни один
тест не требует: клиент подменяется либо фабрикой, либо транспортом httpx.
"""

from __future__ import annotations

import asyncio
import json
import time
import warnings

import httpx
import pytest

from partest_load.configs.endpoints import ConfigKeyWarning, EndpointConfig, parse_endpoints
from partest_load.configs.profiles import parse_profile
from partest_load.core import worker as worker_module
from partest_load.core.client import make_single_request
from partest_load.core.worker import launch_phase_workers
from partest_load.engines import base as base_module
from partest_load.engines.base import MIN_SAMPLE_FOR_STOP, Phase, _threshold_breaches
from partest_load.engines.batch import BatchEngine
from partest_load.engines.linear import LinearEngine
from partest_load.engines.ramp import RampEngine
from partest_load.engines.stress import (
    StressEngine,
    _stress_rate_per_worker,
    _stress_requests_per_worker,
)


class _FakeClient:
    async def __aenter__(self) -> "_FakeClient":
        return self

    async def __aexit__(self, *_exc) -> bool:
        return False


def _ok(i: int = 0, duration: float = 0.01, success: bool = True) -> dict:
    return {
        "timestamp": "2026-10-16T10:00:00+00:00",
        "request_start_ts": 1_700_000_000.0 + i,
        "url": "https://service.invalid/x",
        "method": "GET",
        "status": 200 if success else 500,
        "duration_sec": duration,
        "success": success,
        "size_mb": 0.0,
    }


def _endpoint(**extra) -> EndpointConfig:
    data = {"name": "probe", "base_url": "https://service.invalid", "path": "/x",
            "method": "GET", **extra}
    return EndpointConfig(**data)


# ── stress: уровень поднимает темп ────────────────────────────────────────────────────

STRESS = {"type": "stress", "workers": 10, "rate_limit_rps": 10, "max_requests_per_worker": 30,
          "initial_concurrency": 50, "max_concurrency": 600, "ramp_up_step": 50,
          "ramp_up_interval_sec": 20, "max_duration_sec": 1200}


class TestStressRaisesTheTempo:
    """Раньше уровень поднимал только потолок одновременности, темп оставался профильным."""

    def test_tempo_at_each_level_is_proportional_to_the_level(self):
        profile = parse_profile(STRESS)
        phases = StressEngine(profile, None).plan()
        base_tempo = profile.workers * profile.rate_limit_rps

        assert len(phases) > 2
        for phase in phases:
            tempo = profile.workers * phase.rate_limit_rps
            assert tempo == pytest.approx(base_tempo * phase.concurrency / 50), phase.name

    def test_the_first_level_keeps_the_profile_tempo(self):
        profile = parse_profile(STRESS)
        first = StressEngine(profile, None).plan()[0]

        assert first.rate_limit_rps == pytest.approx(profile.rate_limit_rps)
        assert _stress_rate_per_worker(profile, profile.initial_concurrency) == profile.rate_limit_rps

    def test_every_level_fits_into_what_the_workers_can_hold_in_flight(self):
        """10 воркеров × 30 запросов = 300: уровни выше раньше были недостижимы вовсе."""
        profile = parse_profile(STRESS)
        for phase in StressEngine(profile, None).plan():
            assert profile.workers * phase.max_requests_per_worker >= phase.concurrency, phase.name
        # И профильный предел не понижается там, где его хватает.
        assert _stress_requests_per_worker(profile, 50) == 30

    def test_the_engine_hands_the_level_tempo_to_the_workers(self, monkeypatch):
        """План — не всё: проверяется, что фаза запускается именно с ним."""
        captured = {}

        async def fake_launch(**kwargs):
            captured.update(kwargs)
            return []

        monkeypatch.setattr(base_module, "launch_phase_workers", fake_launch)
        engine = StressEngine(parse_profile(STRESS), None)
        top = engine.plan()[-1]
        asyncio.run(engine._run_phase(top))

        assert captured["rate_limit_delay"] == pytest.approx(1.0 / top.rate_limit_rps)
        assert captured["max_requests_per_worker"] == top.max_requests_per_worker
        assert captured["concurrency"] == top.concurrency


# ── batch: конечный список, а не поток ────────────────────────────────────────────────

class TestBatchSendsAFiniteList:

    def test_the_phase_ends_when_the_list_is_done(self, monkeypatch):
        monkeypatch.setattr(worker_module, "create_http_client", lambda **_kw: _FakeClient())
        sent = []

        async def answer(**kwargs):
            sent.append(dict(kwargs.get("path_params") or {}))
            return _ok(len(sent))

        monkeypatch.setattr(worker_module, "make_single_request", answer)
        endpoint = EndpointConfig(name="probe", base_url="https://service.invalid",
                                  path="/v1/items/{id}", method="GET",
                                  payloads=[{"path_params": {"id": "a"}},
                                            {"path_params": {"id": "b"}}])

        started = time.monotonic()
        results = asyncio.run(launch_phase_workers(
            count=3, concurrency=4, endpoint_cfg=endpoint, phase_duration_sec=30,
            phase_name="batch", rate_limit_delay=0.001, request_budget=6))
        elapsed = time.monotonic() - started

        assert len(results) == 6
        # Каждый вариант — поровну, сколько бы воркеров ни делили список.
        assert sorted(p["id"] for p in sent) == ["a", "a", "a", "b", "b", "b"]
        assert elapsed < 10, "фаза ждала конца предельного времени, а не конца списка"

    def test_a_batch_profile_runs_its_list_through_the_engine(self, monkeypatch):
        captured = {}

        async def fake_launch(**kwargs):
            captured.update(kwargs)
            return []

        monkeypatch.setattr(base_module, "launch_phase_workers", fake_launch)
        profile = parse_profile({"type": "batch", "duration_sec": 60, "target_concurrency": 2,
                                 "repeat_count": 4})
        engine = BatchEngine(profile, _endpoint())

        async def drain():
            return [item async for item in engine.generate_phases()]

        asyncio.run(drain())
        assert captured["request_budget"] == 4  # один вариант × 4 повтора


class TestEndpointKeysThatDoNothingSaySo:

    @pytest.mark.parametrize("key, value", [("batch_mode", True), ("repeat_count", 5)])
    def test_an_ignored_endpoint_key_warns(self, key, value):
        raw = {"items": {"name": "x", "base_url": "https://service.invalid", "path": "/x",
                         "method": "GET", key: value}}
        with pytest.warns(ConfigKeyWarning, match=key):
            parse_endpoints(raw)

    def test_a_plain_endpoint_does_not_warn(self):
        raw = {"items": {"name": "x", "base_url": "https://service.invalid", "path": "/x",
                         "method": "GET"}}
        with warnings.catch_warnings():
            warnings.simplefilter("error", ConfigKeyWarning)
            parse_endpoints(raw)

    def test_the_cli_shows_the_warning_as_a_line(self, tmp_path, capsys):
        from partest_load import cli

        endpoints = tmp_path / "endpoints.yaml"
        endpoints.write_text("items:\n  name: x\n  base_url: https://service.invalid\n"
                             "  path: /x\n  method: GET\n  batch_mode: true\n",
                             encoding="utf-8")
        profile = tmp_path / "steady.yaml"
        profile.write_text("type: linear\nduration_sec: 60\nworkers: 1\n", encoding="utf-8")

        code = cli.main(["--endpoints", str(endpoints), "--endpoint", "items",
                         "--profile", str(profile), "--dry-run"])

        assert code == 0
        err = capsys.readouterr().err
        assert "batch_mode" in err and "не поддерживается" in err


# ── {placeholders} пути ──────────────────────────────────────────────────────────────

class TestPathPlaceholdersAreFilled:

    def test_endpoint_values_are_substituted(self):
        endpoint = _endpoint(path="/v1/items/{id}/parts/{part}",
                             path_params={"id": 42, "part": "top"})
        assert endpoint.url() == "https://service.invalid/v1/items/42/parts/top"

    def test_variant_values_override_the_endpoint_ones(self):
        endpoint = _endpoint(path="/v1/items/{id}", path_params={"id": 1},
                             payloads=[{"path_params": {"id": 7}}])
        assert endpoint.url(endpoint.payloads[0].path_params) == \
            "https://service.invalid/v1/items/7"

    def test_a_value_cannot_change_the_path(self):
        endpoint = _endpoint(path="/v1/items/{id}", path_params={"id": "../admin"})
        assert endpoint.url() == "https://service.invalid/v1/items/..%2Fadmin"

    def test_the_operation_keeps_the_template(self):
        endpoint = _endpoint(path="/v1/items/{id}", path_params={"id": 42})
        assert endpoint.operation() == "GET /v1/items/{id}"

    def test_a_missing_value_is_an_error_naming_the_placeholder(self):
        endpoint = _endpoint(path="/v1/items/{id}")
        with pytest.raises(ValueError, match=r"\{id\}"):
            endpoint.require_path_params()
        with pytest.raises(ValueError, match=r"\{id\}"):
            endpoint.url()

    def test_a_value_for_a_placeholder_that_is_not_there_is_an_error(self):
        with pytest.raises(ValueError, match="item_id"):
            _endpoint(path="/v1/items/{id}", path_params={"item_id": 1})

    def test_the_value_does_not_reach_the_snapshot(self):
        snapshot = _endpoint(path="/v1/items/{id}", path_params={"id": "sentinel-id"}).snapshot()
        assert "sentinel-id" not in repr(snapshot)

    def test_the_request_goes_to_the_filled_path(self):
        seen = []

        def handler(request: httpx.Request) -> httpx.Response:
            seen.append(str(request.url))
            return httpx.Response(200, json={})

        endpoint = _endpoint(path="/v1/items/{id}")

        async def go():
            async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
                return await make_single_request(client, endpoint, None, {"id": 5})

        result = asyncio.run(go())
        assert seen == ["https://service.invalid/v1/items/5"]
        assert result["url"] == seen[0]

    def test_the_cli_refuses_before_any_load(self, monkeypatch, tmp_path, capsys):
        from partest_load import cli

        def _must_not_run(*a, **k):
            raise AssertionError("нагрузка пошла с неподставленным плейсхолдером")

        monkeypatch.setattr(cli, "run_load_test", _must_not_run)
        endpoints = tmp_path / "endpoints.yaml"
        endpoints.write_text("items:\n  name: x\n  base_url: https://service.invalid\n"
                             "  path: /v1/items/{id}\n  method: GET\n", encoding="utf-8")
        profile = tmp_path / "steady.yaml"
        profile.write_text("type: linear\nduration_sec: 60\nworkers: 1\n", encoding="utf-8")
        argv = ["--endpoints", str(endpoints), "--endpoint", "items",
                "--profile", str(profile), "--results-root", str(tmp_path / "out")]

        assert cli.main(argv) == 1
        assert "{id}" in capsys.readouterr().err
        assert cli.main(argv + ["--dry-run"]) == 1


# ── тело запроса не управляет адресом ───────────────────────────────────────────────

class TestTheBodyDoesNotChooseTheAddress:

    def test_full_url_in_the_body_is_just_a_field(self):
        seen = []

        def handler(request: httpx.Request) -> httpx.Response:
            seen.append((str(request.url), json.loads(request.content)))
            return httpx.Response(200, json={})

        with pytest.warns(ConfigKeyWarning, match="full_url"):
            endpoint = _endpoint(method="POST", path="/v1/items",
                                 payloads=[{"body": {"full_url": "https://elsewhere.invalid/y"}}])

        async def go():
            async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
                return await make_single_request(client, endpoint, endpoint.payloads[0].body)

        asyncio.run(go())
        assert seen == [("https://service.invalid/v1/items",
                         {"full_url": "https://elsewhere.invalid/y"})]


# ── --save-raw предупреждает ─────────────────────────────────────────────────────────

class TestSaveRawWarns:

    def _files(self, tmp_path):
        endpoints = tmp_path / "endpoints.yaml"
        endpoints.write_text("items:\n  name: x\n  base_url: https://service.invalid\n"
                             "  path: /x\n  method: GET\n", encoding="utf-8")
        profile = tmp_path / "steady.yaml"
        profile.write_text("type: linear\nduration_sec: 60\nworkers: 1\n", encoding="utf-8")
        return ["--endpoints", str(endpoints), "--endpoint", "items",
                "--profile", str(profile), "--dry-run"]

    def test_the_warning_is_printed_when_enabled(self, tmp_path, capsys):
        from partest_load import cli

        assert cli.main(self._files(tmp_path) + ["--save-raw"]) == 0
        err = capsys.readouterr().err
        assert "--save-raw" in err
        assert "без маскировки" in err

    def test_no_warning_without_the_flag(self, tmp_path, capsys):
        from partest_load import cli

        assert cli.main(self._files(tmp_path)) == 0
        assert "--save-raw" not in capsys.readouterr().err

    def test_the_help_says_what_gets_written(self):
        from partest_load import cli

        text = " ".join(cli.build_parser().format_help().split())
        assert "тела запросов целиком" in text


# ── пороги остановки linear, ramp и batch ─────────────────────────────────────────────

class TestProfileThresholdsStopTheRun:

    def test_no_reason_below_the_minimal_sample(self):
        few = [_ok(i, success=False) for i in range(MIN_SAMPLE_FOR_STOP - 1)]
        assert _threshold_breaches(few, max_error_rate=1.0) == []

    def test_error_rate_at_the_threshold_stops(self):
        window = [_ok(i, success=(i % 10 != 0)) for i in range(200)]  # 10% ошибок
        assert _threshold_breaches(window, max_error_rate=10.0)
        assert _threshold_breaches(window, max_error_rate=10.5) == []

    def test_zero_error_threshold_means_until_the_first_error(self):
        clean = [_ok(i) for i in range(200)]
        assert _threshold_breaches(clean, max_error_rate=0.0) == []
        assert _threshold_breaches(clean + [_ok(201, success=False)], max_error_rate=0.0)

    def test_p95_threshold(self):
        slow = [_ok(i, duration=2.0) for i in range(200)]
        assert _threshold_breaches(slow, max_p95_ms=1500)
        assert _threshold_breaches(slow, max_p95_ms=2500) == []

    @staticmethod
    def _drive(engine, results_per_phase):
        async def fake_run_phase(phase, stop_check=None):
            return list(results_per_phase)

        engine._run_phase = fake_run_phase

        async def drain():
            return [item async for item in engine.generate_phases()]

        return asyncio.run(drain())

    def test_linear_stops_on_its_error_rate(self):
        profile = parse_profile({"type": "linear", "duration_sec": 60, "target_concurrency": 50,
                                 "workers": 2, "ramp_up_sec": 40, "stop_on_error_rate": 5.0})
        engine = LinearEngine(profile, None)
        failing = [_ok(i, success=False) for i in range(200)]

        ran = self._drive(engine, failing)

        assert len(ran) == 1 < len(engine.plan())
        assert engine.stop_reasons

    def test_linear_without_thresholds_runs_the_whole_plan(self):
        profile = parse_profile({"type": "linear", "duration_sec": 60, "target_concurrency": 50,
                                 "workers": 2, "ramp_up_sec": 40})
        engine = LinearEngine(profile, None)
        failing = [_ok(i, success=False) for i in range(200)]

        assert len(self._drive(engine, failing)) == len(engine.plan())
        assert engine.stop_reasons == []

    def test_ramp_stops_on_its_p95(self):
        profile = parse_profile({
            "type": "ramp", "stop_on_p95_latency_ms": 1000,
            "phases": [{"duration_sec": 10, "target_concurrency": 5},
                       {"duration_sec": 10, "target_concurrency": 10},
                       {"duration_sec": 10, "target_concurrency": 15}],
        })
        engine = RampEngine(profile, None)
        slow = [_ok(i, duration=3.0) for i in range(200)]

        assert len(self._drive(engine, slow)) == 1
        assert "p95" in engine.stop_reasons[0]

    def test_the_threshold_stops_a_phase_from_inside(self, monkeypatch):
        """У linear без разгона фаза одна на весь прогон: проверка на границе опоздала бы."""
        monkeypatch.setattr(worker_module, "_STOP_CHECK_INTERVAL_SEC", 0.05)
        monkeypatch.setattr(worker_module, "create_http_client", lambda **_kw: _FakeClient())
        counter = {"n": 0}

        async def failing(**_kwargs):
            counter["n"] += 1
            return _ok(counter["n"], success=False)

        monkeypatch.setattr(worker_module, "make_single_request", failing)

        started = time.monotonic()
        asyncio.run(launch_phase_workers(
            count=2, concurrency=4, endpoint_cfg=_endpoint(), phase_duration_sec=30,
            phase_name="steady", rate_limit_delay=0.001,
            stop_check=lambda tail: ["стоп"] if len(tail) >= 10 else []))

        assert time.monotonic() - started < 10, "фаза дошла до конца, порог не остановил её"

    def test_phase_overrides_default_to_the_profile(self):
        """Поля фазы необязательны: движки, которые их не задают, идут по профилю."""
        phase = Phase("steady", 10, 60)
        assert (phase.rate_limit_rps, phase.max_requests_per_worker, phase.requests) == \
            (None, None, None)
