"""Запуск фазы: что делает библиотека, когда нагрузки не получилось.

Сеть здесь не нужна: проверяется не отправка, а поведение на отказе. Самый дорогой случай —
тихий: фаза, в которой не ушло ни одного запроса, доезжает до метрик как «0 запросов, 0%
ошибок» и ложится в отчёт прогоном, который прошёл.
"""

from __future__ import annotations

import asyncio
import contextlib

import pytest

from partest_load.configs.endpoints import EndpointConfig
from partest_load.core import worker as worker_module
from partest_load.core.worker import (
    PhaseFailedError,
    PhaseState,
    launch_phase_workers,
)


@pytest.fixture()
def endpoint() -> EndpointConfig:
    return EndpointConfig(name="demo", base_url="http://127.0.0.1:9", path="/x",
                          method="POST", timeout=1.0, payloads=[{"body": {"a": 1}}])


def test_phase_that_sent_nothing_is_an_error_not_an_empty_result(endpoint, monkeypatch):
    """Отказ на создании клиента не превращается в пустой успешный прогон.

    Так выглядит, например, `http2=True` без пакета `h2`: ImportError, ноль запросов,
    «0% ошибок» в отчёте. Подменяется фабрика клиента, а не сеть, — проверяется именно
    обработка отказа.
    """
    def refuse(**_kwargs):
        raise ImportError("нет пакета h2")

    monkeypatch.setattr(worker_module, "create_http_client", refuse)

    with pytest.raises(PhaseFailedError, match="ни один из 3 воркеров"):
        asyncio.run(launch_phase_workers(count=3, concurrency=2, endpoint_cfg=endpoint,
                                        phase_duration_sec=0.05, phase_name="steady"))


def test_the_original_reason_is_not_lost(endpoint, monkeypatch):
    def refuse(**_kwargs):
        raise ImportError("нет пакета h2")

    monkeypatch.setattr(worker_module, "create_http_client", refuse)

    with pytest.raises(PhaseFailedError) as info:
        asyncio.run(launch_phase_workers(count=1, concurrency=1, endpoint_cfg=endpoint,
                                        phase_duration_sec=0.05, phase_name="steady"))

    assert isinstance(info.value.__cause__, ImportError)
    assert "h2" in str(info.value)


def test_one_broken_worker_does_not_cancel_the_phase(endpoint, monkeypatch):
    """Упал один из нескольких — фаза продолжается, результаты остальных сохраняются.

    Это обратная сторона предыдущего теста: поднимать тревогу на каждом сбое под нагрузкой
    нельзя, иначе один разорванный сокет обрывает прогон.
    """
    calls = {"n": 0}

    @contextlib.asynccontextmanager
    async def flaky(**kwargs):
        calls["n"] += 1
        if calls["n"] == 1:
            raise OSError("сокет не дали")
        yield object()

    async def refused(*args, **kwargs):
        return {"success": False, "status_code": None, "duration_sec": 0.0,
                "error": "connection refused", "error_type": "network",
                "request_start_ts": 0.0}

    monkeypatch.setattr(worker_module, "create_http_client", flaky)
    # Запрос отвечает отказом сразу и без сети. Прежняя редакция стучалась в закрытый порт
    # петли при длительности фазы 0.05 с — и это была **единственная** нестабильность
    # набора: под загруженной машиной отказ соединения не успевал вернуться внутрь фазы,
    # результатов не оставалось, и тест падал. Дважды за день он уронил ворота, и оба раза
    # пока рядом шла другая работа. Тест про поведение фазы на отказе воркера; сколько
    # миллисекунд операционная система тратит на отклонение соединения — не его предмет.
    monkeypatch.setattr(worker_module, "make_single_request", refused)

    results = asyncio.run(launch_phase_workers(
        count=3, concurrency=8, endpoint_cfg=endpoint,
        phase_duration_sec=0.2, phase_name="steady", rate_limit_delay=0.02))

    # Первый воркер не получил клиента вовсе; двое остальных отдали замеры. Результат —
    # это замер, а не исключение, и фаза обязана его вернуть.
    assert calls["n"] == 3
    assert results
    assert all(r["success"] is False for r in results)


class _FakeClient:
    """Клиент, который никуда не ходит: фазу проверяем без сети."""

    async def __aenter__(self) -> "_FakeClient":
        return self

    async def __aexit__(self, *_exc) -> bool:
        return False


def _result(i: int) -> dict:
    return {
        "timestamp": "2026-09-12T10:00:00+00:00",
        "request_start_ts": 1_700_000_000.0 + i,
        "url": "http://127.0.0.1:9/x",
        "method": "POST",
        "status": 200,
        "duration_sec": 0.01,
        "success": True,
        "size_mb": 0.0,
    }


def _short_drain(monkeypatch) -> None:
    monkeypatch.setattr(worker_module, "PHASE_DRAIN_SEC", 0.2)
    monkeypatch.setattr(worker_module, "PHASE_HARD_DRAIN_SEC", 0.2)


def test_overflowed_phase_without_a_single_measurement_is_an_error(endpoint, monkeypatch):
    """Фаза, из которой не вернулся ни один запрос, не отчитывается пустотой.

    Это тихий отказ, и он приходит ровно тогда, когда стенд просел: запросы ушли, ответы не
    пришли, фазу снимают по сроку. Пустой уровень в отчёте читается как «0 запросов, 0%
    ошибок» — прогон, который прошёл.
    """
    _short_drain(monkeypatch)
    monkeypatch.setattr(worker_module, "create_http_client", lambda **_kw: _FakeClient())

    async def never_answers(**_kwargs):
        await asyncio.sleep(30)
        return _result(0)

    monkeypatch.setattr(worker_module, "make_single_request", never_answers)

    with pytest.raises(PhaseFailedError, match="не успел отдать замер"):
        asyncio.run(launch_phase_workers(count=2, concurrency=4, endpoint_cfg=endpoint,
                                        phase_duration_sec=0.2, phase_name="steady",
                                        rate_limit_delay=0.01))


def test_overflowed_phase_keeps_what_it_already_measured(endpoint, monkeypatch):
    """Снятие задач по сроку не уносит с собой уже собранные замеры.

    `wait_for(gather(...))` по таймауту отменяет сам `gather`, а тот — всех детей: фаза,
    просевшая на середине, возвращала ноль результатов вместо того, что успела измерить.
    """
    _short_drain(monkeypatch)
    monkeypatch.setattr(worker_module, "create_http_client", lambda **_kw: _FakeClient())

    answered = {"n": 0}

    async def two_fast_then_silence(**_kwargs):
        answered["n"] += 1
        if answered["n"] > 2:
            await asyncio.sleep(30)
        return _result(answered["n"])

    monkeypatch.setattr(worker_module, "make_single_request", two_fast_then_silence)

    results = asyncio.run(launch_phase_workers(
        count=2, concurrency=4, endpoint_cfg=endpoint, phase_duration_sec=0.2,
        phase_name="steady", rate_limit_delay=0.01))

    assert len(results) == 2
    assert all(r["phase"] == "steady" for r in results)


def test_phase_state_tracks_the_peak_not_the_current_value():
    state = PhaseState()

    state.enter()
    state.enter()
    state.leave()
    state.leave()

    assert state.active == 0
    assert state.peak == 2


def test_phase_state_never_goes_below_zero():
    state = PhaseState()
    state.leave()

    assert state.active == 0
