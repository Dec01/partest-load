"""Пороги остановки и события прогона; разбор конфигурации без утечки значений.

Сети ни один тест не требует: фазы подменяются на уровне движка или фабрики клиента.
"""

from __future__ import annotations

import asyncio
import logging

import pytest

from partest_load import cli
from partest_load.configs.endpoints import EndpointConfig
from partest_load.configs.profiles import parse_profile
from partest_load.core import runner as runner_mod
from partest_load.core.metrics import RunMetrics
from partest_load.core.worker import _finished_at, _latest_finished
from partest_load.engines import base as base_module
from partest_load.engines.base import MIN_SAMPLE_FOR_STOP, _rate_breached, _threshold_breaches
from partest_load.engines.batch import BatchEngine
from partest_load.engines.linear import LinearEngine
from partest_load.engines.stress import StressEngine, saturation_point, stop_reasons

HEADER_CANARY = "canary-not-a-credential"


def _r(start: float, duration: float = 0.01, success: bool = True, **extra) -> dict:
    return {"timestamp": "2026-10-16T10:00:00+00:00", "request_start_ts": start,
            "url": "https://service.invalid/x", "method": "GET",
            "status": 200 if success else 500, "duration_sec": duration,
            "success": success, "size_mb": 0.0, **extra}


def _files(tmp_path, endpoints_yaml: str, profile_yaml: str = "type: linear\n"
           "duration_sec: 60\nworkers: 1\n"):
    endpoints = tmp_path / "endpoints.yaml"
    endpoints.write_text(endpoints_yaml, encoding="utf-8")
    profile = tmp_path / "profile.yaml"
    profile.write_text(profile_yaml, encoding="utf-8")
    return ["--endpoints", str(endpoints), "--endpoint", "items", "--profile", str(profile),
            "--results-root", str(tmp_path / "out")]


def _drain(engine):
    async def go():
        return [item async for item in engine.generate_phases()]
    return asyncio.run(go())


def _fake_phases(engine, results):
    """Фаза без сети. Если движок дал проверку порогов, она зовётся, как это сделала бы
    проверка внутри фазы, — на всём, что фаза вернула."""
    async def fake_run_phase(phase, stop_check=None):
        out = [dict(r) for r in results]
        if stop_check is not None:
            stop_check(out)
        return out
    engine._run_phase = fake_run_phase


# ── разбор не цитирует значения ──────────────────────────────────────────────────────

class TestErrorsDoNotQuoteTheConfig:

    def test_a_validation_error_does_not_print_header_values(self, tmp_path, capsys):
        argv = _files(tmp_path, "items:\n  name: x\n  base_url: https://service.invalid\n"
                                "  path: /x\n  method: GET\n  timeout: 0.1\n"
                                f"  headers: {{Authorization: {HEADER_CANARY}}}\n"
                                "  payloads:\n    - body: {password: sentinel-body-value}\n"
                                "      path_params: wrong\n")
        assert cli.main(argv + ["--dry-run"]) == 1
        out = capsys.readouterr()
        assert HEADER_CANARY not in out.err + out.out
        assert "sentinel-body-value" not in out.err + out.out
        assert "timeout" in out.err  # что именно не так — по-прежнему видно

    def test_a_broken_endpoints_yaml_shows_only_the_place(self, tmp_path, capsys):
        argv = _files(tmp_path, "items:\n  name: x\n"
                                f"  headers: {{Authorization: {HEADER_CANARY}\n  path: /x\n")
        assert cli.main(argv + ["--dry-run"]) == 1
        err = capsys.readouterr().err
        assert HEADER_CANARY not in err
        assert "строка" in err and "колонка" in err

    def test_a_broken_profile_yaml_shows_only_the_place(self, tmp_path, capsys):
        argv = _files(tmp_path, "items:\n  name: x\n  base_url: https://service.invalid\n"
                                "  path: /x\n  method: GET\n",
                      profile_yaml=f"type: linear\nname: [{HEADER_CANARY}\nworkers: 1\n")
        assert cli.main(argv + ["--dry-run"]) == 1
        err = capsys.readouterr().err
        assert HEADER_CANARY not in err
        assert "строка" in err

    def test_warnings_are_printed_even_when_parsing_fails(self, tmp_path, capsys):
        argv = _files(tmp_path, "items:\n  name: x\n  base_url: https://service.invalid\n"
                                "  path: /x\n  method: GET\n  batch_mode: true\n  timeout: 0.1\n")
        assert cli.main(argv + ["--dry-run"]) == 1
        assert "batch_mode" in capsys.readouterr().err


class TestDotSegmentsAndBlankValues:

    @pytest.mark.parametrize("value, segment", [(".", "%2E"), ("..", "%2E%2E")])
    def test_dot_values_stay_one_segment(self, value, segment):
        endpoint = EndpointConfig(name="x", base_url="https://service.invalid/api",
                                  path="/v1/items/{id}", method="GET", path_params={"id": value})
        assert endpoint.url() == f"https://service.invalid/api/v1/items/{segment}"

    @pytest.mark.parametrize("value", ["", "   "])
    def test_a_blank_value_is_refused_at_parse(self, value):
        with pytest.raises(ValueError, match="пустое значение"):
            EndpointConfig(name="x", base_url="https://service.invalid", path="/v1/items/{id}",
                           method="GET", payloads=[{"path_params": {"id": value}}])


class TestSnapshotKeepsNamesOnly:

    def test_query_values_and_userinfo_do_not_reach_the_snapshot(self):
        endpoint = EndpointConfig(name="x", base_url="https://user:sentinel-pass@service.invalid",
                                  path="/x", method="GET",
                                  query_params={"api_key": "sentinel-query-value"})
        dumped = repr(endpoint.snapshot())

        assert "sentinel-query-value" not in dumped
        assert "sentinel-pass" not in dumped and "user:" not in dumped
        assert "api_key" in dumped
        assert endpoint.snapshot()["base_url"].startswith("https://service.invalid")


def test_the_save_raw_warning_names_the_url_with_path_values(tmp_path, capsys):
    argv = _files(tmp_path, "items:\n  name: x\n  base_url: https://service.invalid\n"
                            "  path: /x\n  method: GET\n")
    assert cli.main(argv + ["--dry-run", "--save-raw"]) == 0
    assert "path_params" in capsys.readouterr().err


# ── окно порогов, события остановки, batch ────────────────────────────────────────────

class TestTheWindowIsOrderedByCompletion:

    def test_latest_finished_takes_slow_requests_that_ended_last(self):
        slow = _r(start=0.0, duration=100.0, success=False)
        fast = [_r(start=50.0 + i) for i in range(5)]
        assert _latest_finished(fast + [slow], 1) == [slow]
        assert _finished_at(slow) == 100.0

    def test_late_failures_stop_the_run(self):
        """Ошибки начаты рано, а вернулись (таймаутом) позже всех успешных."""
        failures = [_r(start=float(i), duration=1000.0, success=False) for i in range(150)]
        successes = [_r(start=200.0 + i) for i in range(300)]
        # По началу последние 300 — одни успешные; по завершению половина окна — ошибки.
        assert _threshold_breaches(sorted(failures + successes,
                                         key=lambda r: r["request_start_ts"])[-300:],
                                  max_error_rate=40.0) == []

        profile = parse_profile({"type": "linear", "duration_sec": 60, "target_concurrency": 50,
                                 "workers": 2, "ramp_up_sec": 40, "stop_on_error_rate": 40.0})
        engine = LinearEngine(profile, None)
        _fake_phases(engine, failures + successes)

        assert len(_drain(engine)) == 1
        assert engine.stop_reasons


class TestStopIsVisible:

    def test_the_runner_writes_a_stopped_event_and_the_summary_shows_it(self, monkeypatch, capsys):
        async def failing(self, phase, stop_check=None):
            return [_r(float(i), success=False) for i in range(200)]

        monkeypatch.setattr(LinearEngine, "_run_phase", failing)
        profile = parse_profile({"type": "linear", "duration_sec": 60, "target_concurrency": 50,
                                 "workers": 2, "ramp_up_sec": 40, "stop_on_error_rate": 5.0})
        endpoint = EndpointConfig(name="x", base_url="https://service.invalid", path="/x",
                                  method="GET")
        _results, metrics, events = asyncio.run(runner_mod.run_load_test(endpoint, profile))

        stopped = [e for e in events if e.get("event") == "stopped"]
        assert len(stopped) == 1
        assert stopped[0]["phase"] == "ramp-up-1" and stopped[0]["reasons"]

        from partest_load.storage.schema import AggregatedMetrics
        cli.print_summary(AggregatedMetrics.from_run_metrics(metrics), events=events)
        assert "ОСТАНОВЛЕН" in capsys.readouterr().out

    def test_stress_fills_stop_reasons_when_the_ladder_breaks(self):
        profile = parse_profile({"type": "stress", "workers": 2, "initial_concurrency": 10,
                                 "max_concurrency": 100, "ramp_up_step": 10,
                                 "ramp_up_interval_sec": 5, "hold_after_breaking_sec": 0})
        engine = StressEngine(profile, None)
        _fake_phases(engine, [_r(float(i), success=False) for i in range(200)])

        assert len(_drain(engine)) == 1
        assert engine.stop_reasons
        assert engine.events[0]["event"] == "stopped"


class TestBatchVerdict:

    def _engine(self, **profile):
        endpoint = EndpointConfig(name="x", base_url="https://service.invalid", path="/x",
                                  method="GET")
        return BatchEngine(parse_profile({"type": "batch", "duration_sec": 5,
                                          "repeat_count": 4, **profile}), endpoint)

    def test_a_list_cut_by_time_is_reported(self, caplog):
        engine = self._engine()
        _fake_phases(engine, [_r(0.0), _r(1.0)])
        with caplog.at_level(logging.WARNING):
            _drain(engine)

        assert "не пройден" in caplog.text
        event = engine.events[-1]
        assert (event["event"], event["planned"], event["completed"]) == ("batch_list", 4, 2)
        assert "предел времени" in event["reason"]

    def test_a_list_cut_by_a_threshold_names_the_threshold(self):
        """Список длиннее окна: порог успевает оборвать его до конца."""
        endpoint = EndpointConfig(name="x", base_url="https://service.invalid", path="/x",
                                  method="GET", payloads=[{}, {}])
        engine = BatchEngine(parse_profile({"type": "batch", "repeat_count": 100,
                                            "stop_on_error_rate": 10.0}), endpoint)
        _fake_phases(engine, [_r(float(i), success=(i % 2 == 0)) for i in range(160)])
        _drain(engine)

        batch = [e for e in engine.events if e["event"] == "batch_list"][0]
        assert (batch["planned"], batch["completed"]) == (200, 160)
        assert "порог" in batch["reason"]
        assert any(e["event"] == "stopped" for e in engine.events)

    def test_a_done_list_has_no_reason(self):
        engine = self._engine()
        _fake_phases(engine, [_r(float(i)) for i in range(4)])
        _drain(engine)
        assert engine.events[-1]["reason"] is None


class TestDeadThresholdsAreNamed:

    def test_a_short_batch_uses_the_list_as_its_sample(self):
        engine = BatchEngine(parse_profile({"type": "batch", "stop_on_error_rate": 10.0}),
                             EndpointConfig(name="x", base_url="https://service.invalid",
                                            path="/x", method="GET"))
        assert engine._min_sample() == 3 < MIN_SAMPLE_FOR_STOP
        # Порог такого списка не обрывает, а отмечает итог — и план говорит об этом.
        notes = engine.plan_notes()
        assert len(notes) == 1 and "не обрывает" in notes[0]

    def test_a_short_batch_breach_is_breached_not_stopped(self, capsys):
        engine = BatchEngine(parse_profile({"type": "batch", "stop_on_error_rate": 10.0}),
                             EndpointConfig(name="x", base_url="https://service.invalid",
                                            path="/x", method="GET"))
        _fake_phases(engine, [_r(0.0, success=False), _r(1.0), _r(2.0)])
        _drain(engine)

        kinds = [e["event"] for e in engine.events]
        assert "breached" in kinds and "stopped" not in kinds
        assert engine.events[0]["reason"] is None  # список пройден целиком

        from partest_load.storage.schema import AggregatedMetrics
        cli.print_summary(AggregatedMetrics.from_run_metrics(RunMetrics()), events=engine.events)
        out = capsys.readouterr().out
        assert "ПОРОГ ДОСТИГНУТ" in out and "ОСТАНОВЛЕН" not in out

    def test_a_breach_in_the_last_linear_phase_stops_nothing(self):
        profile = parse_profile({"type": "linear", "duration_sec": 60, "workers": 2,
                                 "ramp_up_sec": 0, "stop_on_error_rate": 5.0})
        engine = LinearEngine(profile, None)

        async def finished_phase(phase, stop_check=None):
            return [_r(float(i), success=False) for i in range(200)]  # проверка внутри не сработала

        engine._run_phase = finished_phase
        _drain(engine)
        assert [e["event"] for e in engine.events] == ["breached"]

    def test_a_linear_threshold_that_cannot_fire_is_named_before_the_run(self):
        low = LinearEngine(parse_profile({"type": "linear", "duration_sec": 5, "workers": 1,
                                          "rate_limit_rps": 1, "ramp_up_sec": 0,
                                          "stop_on_error_rate": 5.0}), None)
        assert low.plan_notes() and "stop_on_error_rate" in low.plan_notes()[0]

        enough = LinearEngine(parse_profile({"type": "linear", "duration_sec": 60, "workers": 5,
                                             "rate_limit_rps": 10, "ramp_up_sec": 0,
                                             "stop_on_error_rate": 5.0}), None)
        assert enough.plan_notes() == []


class TestDryRunShowsThePlan:

    def test_stress_plan_with_tempo(self, tmp_path, capsys):
        argv = _files(tmp_path, "items:\n  name: x\n  base_url: https://service.invalid\n"
                                "  path: /x\n  method: GET\n",
                      profile_yaml="type: stress\nworkers: 10\nrate_limit_rps: 10\n"
                                   "initial_concurrency: 50\nmax_concurrency: 100\n"
                                   "ramp_up_step: 50\n")
        assert cli.main(argv + ["--dry-run"]) == 0
        out = capsys.readouterr().out
        assert "план:" in out
        assert "stress-lvl-100" in out and "200.0" in out  # 10 воркеров × 10 × 100/50

    def test_batch_plan_shows_the_list_size(self, tmp_path, capsys):
        argv = _files(tmp_path, "items:\n  name: x\n  base_url: https://service.invalid\n"
                                "  path: /x\n  method: GET\n",
                      profile_yaml="type: batch\n")
        assert cli.main(argv + ["--dry-run"]) == 0
        assert "список: 3 запросов" in capsys.readouterr().out


# ── одно определение порогов ──────────────────────────────────────────────────────────

class TestOneDefinitionOfAThreshold:

    def test_zero_means_until_the_first_case_everywhere(self):
        assert _rate_breached(0.0, 0.0) is False
        assert _rate_breached(0.1, 0.0) is True
        clean = RunMetrics(error_rate_pct=0.0, p99_ms=10.0)
        assert stop_reasons(clean, sample_size=300, max_error_rate=0.0, max_p99_ms=1e9,
                            max_5xx_rate=0.0) == []
        assert saturation_point([(10, [{"success": True}] * 50)], max_error_rate=0.0) is None

    def test_stress_and_linear_agree_on_the_same_window(self):
        window = [_r(float(i), success=(i % 10 != 0)) for i in range(300)]
        assert _threshold_breaches(window, max_error_rate=10.0)
        metrics = RunMetrics.from_results(window, 30.0)
        assert stop_reasons(metrics, sample_size=300, max_error_rate=10.0, max_p99_ms=1e9,
                            max_5xx_rate=100.0)


# ── проводка: проверки стоят там, где нужны ──────────────────────────────────────────

class TestWiring:

    def test_run_load_test_refuses_before_the_first_phase(self, monkeypatch):
        class _MustNotRun:
            def __init__(self, *a, **k):
                pass

            def generate_phases(self):
                raise AssertionError("фаза началась с неподставленным плейсхолдером")

        monkeypatch.setitem(runner_mod.ENGINES, "linear", _MustNotRun)
        endpoint = EndpointConfig(name="x", base_url="https://service.invalid",
                                  path="/v1/items/{id}", method="GET")
        profile = parse_profile({"type": "linear", "duration_sec": 1, "workers": 1})

        with pytest.raises(ValueError, match=r"\{id\}"):
            asyncio.run(runner_mod.run_load_test(endpoint, profile))

    def test_the_cli_refusal_does_not_depend_on_printing_the_address(self, monkeypatch,
                                                                     tmp_path, capsys):
        monkeypatch.setattr(EndpointConfig, "url", lambda self, path_params=None: "https://x")
        monkeypatch.setattr(cli, "run_load_test", lambda *a, **k: pytest.fail("нагрузка пошла"))
        argv = _files(tmp_path, "items:\n  name: x\n  base_url: https://service.invalid\n"
                                "  path: /v1/items/{id}\n  method: GET\n")
        assert cli.main(argv + ["--dry-run"]) == 1
        assert "{id}" in capsys.readouterr().err

    @pytest.mark.parametrize("threshold, wired", [({"stop_on_error_rate": 5.0}, True), ({}, False)])
    def test_the_engine_hands_its_thresholds_to_the_phase(self, monkeypatch, threshold, wired):
        captured = {}

        async def fake_launch(**kwargs):
            captured.update(kwargs)
            return []

        monkeypatch.setattr(base_module, "launch_phase_workers", fake_launch)
        engine = LinearEngine(parse_profile({"type": "linear", "duration_sec": 1, "workers": 1,
                                             "ramp_up_sec": 0, **threshold}), None)
        _drain(engine)

        assert callable(captured["stop_check"]) is wired
