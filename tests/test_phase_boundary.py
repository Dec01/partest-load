"""Граница фазы: очередь, снятые запросы, отмена; авторизация и адрес базы; журналы.

Всё, что может зависнуть при регрессии, ограничено внешним `wait_for`: сломанная проверка
должна падать, а не вешать набор.
"""

from __future__ import annotations

import asyncio
import logging
import time

import httpx
import pytest

from partest_load import cli
from partest_load.configs.endpoints import EndpointConfig
from partest_load.configs.profiles import ProfileError, parse_profile
from partest_load.core import worker as worker_module
from partest_load.core.client import make_single_request
from partest_load.core.worker import launch_phase_workers
from partest_load.engines.batch import BatchEngine
from partest_load.engines.stress import StressEngine

GUARD_SEC = 20


class _FakeClient:
    async def __aenter__(self) -> "_FakeClient":
        return self

    async def __aexit__(self, *_exc) -> bool:
        return False


def _endpoint(**extra) -> EndpointConfig:
    data = {"name": "x", "base_url": "https://service.invalid", "path": "/x", "method": "GET",
            **extra}
    return EndpointConfig(**data)


def _answer(i: float, success: bool = True) -> dict:
    return {"timestamp": "2026-10-18T10:00:00+00:00", "request_start_ts": 1_700_000_000.0 + i,
            "url": "https://service.invalid/x", "method": "GET", "status": 200,
            "duration_sec": 0.01, "success": success, "size_mb": 0.0}


def _slow_stand(monkeypatch, delay: float):
    """Живой, но медленный стенд: каждый вызов отвечает через `delay` секунд."""
    calls = []
    monkeypatch.setattr(worker_module, "create_http_client", lambda **_kw: _FakeClient())

    async def slow(**_kwargs):
        calls.append(time.monotonic())
        await asyncio.sleep(delay)
        return _answer(len(calls))

    monkeypatch.setattr(worker_module, "make_single_request", slow)
    return calls


def _guarded(coro):
    return asyncio.run(asyncio.wait_for(coro, timeout=GUARD_SEC))


# ── очередь семафора не уходит после конца фазы ───────────────────────────────────

def test_the_queue_does_not_go_to_the_stand_after_the_phase(monkeypatch):
    calls = _slow_stand(monkeypatch, delay=0.3)
    duration = 1.0

    async def run():
        started = time.monotonic()
        results = await launch_phase_workers(
            count=2, concurrency=2, endpoint_cfg=_endpoint(), phase_duration_sec=duration,
            phase_name="steady", max_requests_per_worker=10_000, rate_limit_delay=0.005)
        return started, time.monotonic(), results

    started, ended, results = _guarded(run())

    late = [t for t in calls if t > started + duration + 0.05]
    assert late == [], f"после конца фазы ушло {len(late)} запросов из очереди"
    assert not [r for r in results if r.get("error_type") == "cancelled"]
    assert ended - started < duration + 1.0


def test_the_queue_does_not_go_after_a_threshold_stop(monkeypatch):
    monkeypatch.setattr(worker_module, "_STOP_CHECK_INTERVAL_SEC", 0.05)
    calls = _slow_stand(monkeypatch, delay=0.2)

    async def run():
        started = time.monotonic()
        await launch_phase_workers(
            count=2, concurrency=2, endpoint_cfg=_endpoint(), phase_duration_sec=30,
            phase_name="steady", max_requests_per_worker=10_000, rate_limit_delay=0.005,
            stop_check=lambda tail: ["стоп"] if len(tail) >= 4 else [])
        return time.monotonic() - started

    elapsed = _guarded(run())
    assert elapsed < 3, f"очередь после порога шла ещё {elapsed:.1f} с"
    assert len(calls) < 20


# ── batch: снятые не считаются вернувшимися ──────────────────────────────────────

def test_cancelled_requests_do_not_complete_a_batch_list():
    engine = BatchEngine(parse_profile({"type": "batch", "repeat_count": 4}), _endpoint())
    cancelled = {**_answer(2, success=False), "error_type": "cancelled"}

    async def phase(phase, stop_check=None):
        return [_answer(0), _answer(1), cancelled, dict(cancelled)]

    engine._run_phase = phase

    async def drain():
        return [item async for item in engine.generate_phases()]

    asyncio.run(drain())
    event = engine.events[-1]
    assert (event["planned"], event["completed"]) == (4, 2)
    assert "предел времени" in event["reason"]


# ── авторизация на запросе ────────────────────────────────────────────────────

def _send(endpoint: EndpointConfig):
    seen = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200)

    async def go():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            await make_single_request(client, endpoint)

    asyncio.run(go())
    return seen[0]


def test_a_callers_client_still_carries_the_base_credentials():
    request = _send(_endpoint(base_url="https://user:pw@service.invalid"))
    assert request.headers["Authorization"].startswith("Basic ")
    assert "@" not in str(request.url)


def test_empty_userinfo_does_not_override_an_explicit_authorization():
    """Разбор пустой userinfo и так убирает; сырой адрес (`model_construct`) — нет."""
    parsed = _endpoint(base_url="https://@service.invalid",
                       headers={"Authorization": "Bearer explicit"})
    raw = EndpointConfig.model_construct(**{**parsed.model_dump(),
                                            "base_url": "https://:@service.invalid"})
    for endpoint in (parsed, raw):
        assert endpoint.basic_auth() is None
        assert _send(endpoint).headers["Authorization"] == "Bearer explicit"


def test_userinfo_is_percent_decoded():
    endpoint = _endpoint(base_url="https://us%40er:p%3Ass@service.invalid")
    assert endpoint.basic_auth() == ("us@er", "p:ss")


# ── отмена снаружи снимает фазу целиком ──────────────────────────────────────────

def test_an_outer_cancel_leaves_no_workers_behind(monkeypatch):
    _slow_stand(monkeypatch, delay=5.0)

    async def run():
        started = time.monotonic()
        with pytest.raises(asyncio.TimeoutError):
            await asyncio.wait_for(launch_phase_workers(
                count=2, concurrency=4, endpoint_cfg=_endpoint(), phase_duration_sec=30,
                phase_name="steady", rate_limit_delay=0.01,
                stop_check=lambda tail: []), timeout=0.3)
        elapsed = time.monotonic() - started
        await asyncio.sleep(0)
        phase_coroutines = {"run_worker_phase", "_tracked_request", "watch"}
        return elapsed, [t for t in asyncio.all_tasks()
                         if not t.done() and t.get_coro().__name__ in phase_coroutines]

    elapsed, leftovers = _guarded(run())
    assert leftovers == []
    # Снимает, а не дожидается: висящие запросы стенда (5 с) отмену не задерживают.
    assert elapsed < 2, f"отмена фазы ждала висящие запросы {elapsed:.1f} с"


# ── адрес базы ────────────────────────────────────────────────────────────────

def test_credentials_set_after_creation_do_not_leak():
    endpoint = _endpoint()
    endpoint.base_url = "http://u:sentinel-pw@service.invalid"
    copied = endpoint.model_copy(update={"base_url": "http://u:sentinel-two@service.invalid"})
    built = EndpointConfig.model_construct(**{**endpoint.model_dump(),
                                              "base_url": "http://u:sentinel-3@service.invalid"})

    for item in (endpoint, copied, built):
        assert "sentinel" not in item.url() and "sentinel" not in repr(item)
    assert endpoint.basic_auth() == ("u", "sentinel-pw")


@pytest.mark.parametrize("base", ["https://service.invalid/?", "https://service.invalid/#"])
def test_empty_query_or_fragment_separators_are_refused(base):
    with pytest.raises(ValueError, match="query_params"):
        _endpoint(base_url=base)


# ── тег профиля не цитируется ───────────────────────────────────────────────────

def test_an_unknown_profile_type_is_not_quoted():
    with pytest.raises(ProfileError) as info:
        parse_profile({"type": "sentinel-tag", "workers": 1})
    assert "sentinel" not in str(info.value)
    assert "неизвестный тип профиля" in str(info.value)


# ── журналы h2 и hpack ───────────────────────────────────────────────────────────

def test_h2_and_hpack_loggers_stay_quiet_even_on_debug(tmp_path):
    endpoints = tmp_path / "e.yaml"
    endpoints.write_text("items:\n  name: x\n  base_url: https://service.invalid\n"
                         "  path: /x\n  method: GET\n", encoding="utf-8")
    profile = tmp_path / "p.yaml"
    profile.write_text("type: linear\nduration_sec: 60\nworkers: 1\n", encoding="utf-8")
    try:
        assert cli.main(["--endpoints", str(endpoints), "--endpoint", "items", "--profile",
                         str(profile), "--dry-run", "--log-level", "DEBUG"]) == 0
        for name in ("httpx", "httpcore", "h2", "hpack"):
            assert logging.getLogger(name).level >= logging.WARNING, name
    finally:
        for name in ("httpx", "httpcore", "h2", "hpack"):
            logging.getLogger(name).setLevel(logging.NOTSET)


# ── план в выводе, удержание после отказа, userinfo ────────────────────────────────────────────────────────────

def _cli_files(tmp_path, profile_yaml: str):
    endpoints = tmp_path / "e.yaml"
    endpoints.write_text("items:\n  name: x\n  base_url: https://service.invalid\n"
                         "  path: /x\n  method: GET\n", encoding="utf-8")
    profile = tmp_path / "p.yaml"
    profile.write_text(profile_yaml, encoding="utf-8")
    return ["--endpoints", str(endpoints), "--endpoint", "items", "--profile", str(profile),
            "--results-root", str(tmp_path / "out")]


DEAD_THRESHOLD = ("type: linear\nduration_sec: 5\nworkers: 1\nrate_limit_rps: 1\n"
                  "ramp_up_sec: 0\nstop_on_error_rate: 5\n")


def test_dry_run_prints_plan_notes(tmp_path, capsys):
    assert cli.main(_cli_files(tmp_path, DEAD_THRESHOLD) + ["--dry-run"]) == 0
    assert "внимание" in capsys.readouterr().out


def test_a_real_run_leaves_plan_notes_to_the_journal(tmp_path, monkeypatch, capsys):
    from partest_load.core.metrics import RunMetrics

    async def fake_run(endpoint_cfg, profile):
        return [], RunMetrics(), []

    monkeypatch.setattr(cli, "run_load_test", fake_run)
    assert cli.main(_cli_files(tmp_path, DEAD_THRESHOLD)) == 0
    assert "внимание" not in capsys.readouterr().out


def test_a_breach_on_the_last_stress_level_is_breached_and_still_held():
    profile = parse_profile({"type": "stress", "workers": 2, "initial_concurrency": 10,
                             "max_concurrency": 10, "hold_after_breaking_sec": 5})
    engine = StressEngine(profile, None)
    ran = []

    async def failing(phase, stop_check=None):
        ran.append(phase.name)
        return [{"request_start_ts": float(i), "duration_sec": 0.01, "success": False}
                for i in range(200)]

    engine._run_phase = failing

    async def drain():
        return [item async for item in engine.generate_phases()]

    asyncio.run(drain())
    assert [e["event"] for e in engine.events] == ["breached"]
    assert ran == ["stress-lvl-10", "hold-after-break"]


# ── адрес в обход разбора, снимок, вариант тела, завершение задач ──────────────────────────────────────────────────────────

def test_query_and_fragment_that_bypass_parsing_do_not_leak():
    """`model_copy(update=…)` и `model_construct` разбор обходят — обрезка на выходе."""
    endpoint = _endpoint(base_url="https://service.invalid/api")
    copied = endpoint.model_copy(update={"base_url": "https://service.invalid/api?tok=sentinel#f"})
    built = EndpointConfig.model_construct(**{**dict(endpoint),
                                              "base_url": "https://service.invalid/?tok=sentinel"})
    for item in (copied, built):
        assert "sentinel" not in item.url() and "#" not in item.url()
        assert "sentinel" not in repr(item)
        assert "sentinel" not in repr(item.snapshot())
    assert copied.url() == "https://service.invalid/api/x"


def test_a_snapshot_of_a_constructed_config_raises_no_serializer_warning():
    import warnings

    built = EndpointConfig.model_construct(**{**dict(_endpoint()),
                                              "base_url": "https://service.invalid"})
    with warnings.catch_warnings():
        warnings.simplefilter("error")
        assert built.snapshot()["base_url"] == "https://service.invalid"


def test_a_payload_variant_error_does_not_quote_its_input():
    from pydantic import ValidationError

    from partest_load.configs.endpoints import PayloadVariant

    with pytest.raises(ValidationError) as info:
        PayloadVariant(body="S3CR-body-value")
    assert "S3CR" not in str(info.value)


def test_a_cancelled_worker_has_finished_its_requests_when_it_returns(monkeypatch):
    """Снятые задачи не только отменены, но и завершены к выходу воркера.

    Смотрится в той же задаче, сразу на выходе из `run_worker_phase`, без единого тика
    цикла: вызывающий, проснувшийся позже, увидел бы их завершёнными и без ожидания.
    """
    _slow_stand(monkeypatch, delay=30.0)
    seen = {}

    async def worker(sink):
        try:
            await worker_module.run_worker_phase(
                worker_id="w1", concurrency=4, endpoint_cfg=_endpoint(), phase_duration_sec=30,
                rate_limit_delay=0.01, results_sink=sink)
        except asyncio.CancelledError:
            seen["unfinished"] = [t for t in asyncio.all_tasks()
                                  if t.get_coro().__name__ == "_tracked_request"
                                  and not t.done()]
            seen["cancelled"] = sum(1 for r in sink if r.get("error_type") == "cancelled")
            raise

    async def run():
        sink = []
        task = asyncio.create_task(worker(sink))
        await asyncio.sleep(0.2)
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass

    _guarded(run())
    assert seen["unfinished"] == []
    assert seen["cancelled"] > 0
