"""Воркеры одной фазы: отправка, учёт параллельности, аккуратная остановка.

Одно отличие от прототипа стоит назвать отдельно. Раньше фаза получала готовый
`asyncio.Semaphore`, причём под именем `global_semaphore`, а три движка из четырёх
передавали его как `semaphore=` — то есть падали с TypeError на первой же фазе. Теперь
фаза получает число, а семафор создаёт сама: имени, которое можно перепутать, больше нет.
"""

from __future__ import annotations

import asyncio
import logging
import random
import time
import uuid
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from ..configs.endpoints import EndpointConfig
from .client import create_http_client, make_single_request
from .pacing import next_request_delay

log = logging.getLogger(__name__)

# Запас соединений сверх заданной параллельности: пул не должен становиться вторым,
# незаметным ограничителем нагрузки.
CONNECTION_HEADROOM = 100

# Сколько ждать завершения уже отправленных запросов после конца фазы.
PHASE_DRAIN_SEC = 10.0
PHASE_HARD_DRAIN_SEC = 6.0


class PhaseFailedError(RuntimeError):
    """Фаза не дала ни одного замера: воркеры упали или не уложились в срок.

    Второй случай — переполнение: запросы ушли, но ни один не вернулся за отведённое время
    плюс `PHASE_DRAIN_SEC`. Наблюдаемо это то же самое, что и первый случай, и молчать об
    этом нельзя по той же причине.

    Отдельная ошибка нужна потому, что тихий вариант этого случая дороже громкого. Один
    упавший воркер фазу не отменяет — остальные продолжают, и это правильно. Но если упали
    все, пустая фаза доезжает до метрик как «0 запросов, 0% ошибок», ложится в отчёт и
    выглядит прогоном, который прошёл. Так, например, выглядит отсутствие пакета `h2` при
    `http2=True`: ImportError на создании клиента, ноль запросов, зелёный отчёт.
    """


@dataclass
class PhaseState:
    """Наблюдаемое состояние фазы: сколько запросов в полёте и каков был пик."""

    active: int = 0
    peak: int = 0
    request_starts: List[float] = field(default_factory=list)

    def enter(self) -> int:
        self.active += 1
        self.peak = max(self.peak, self.active)
        return self.active

    def leave(self) -> None:
        self.active = max(0, self.active - 1)


async def _tracked_request(
    client,
    endpoint_cfg: EndpointConfig,
    payload: Optional[Dict[str, Any]],
    global_semaphore: asyncio.Semaphore,
    local_semaphore: asyncio.Semaphore,
    state: PhaseState,
    phase_name: str,
    worker_id: str,
    sink: List[Dict[str, Any]],
) -> Dict[str, Any]:
    try:
        async with global_semaphore:
            current_active = state.enter()
            req_start_ts = time.time()
            state.request_starts.append(req_start_ts)
            try:
                result = await make_single_request(
                    client=client,
                    endpoint_cfg=endpoint_cfg,
                    payload=payload,
                )
                result.update({
                    "worker_id": worker_id,
                    "phase": phase_name,
                    "active_concurrency": current_active,
                })
                # Замер складывается в общий список сразу, а не возвращается наружу через
                # значение задачи. Переполненную фазу снимают принудительно, и всё, что
                # живёт только в возвращаемом значении отменённой задачи, теряется — а
                # теряются именно те замеры, ради которых прогон и делали.
                sink.append(result)
                return result
            finally:
                state.leave()
    finally:
        # Местный семафор держится всё время запроса, а не только на создании задачи.
        # В прототипе он освобождался сразу же, и max_requests_per_worker не ограничивал
        # ничего: объявленная ручка молча ничего не делала.
        local_semaphore.release()


async def run_worker_phase(
    worker_id: str,
    concurrency: int,
    endpoint_cfg: EndpointConfig,
    phase_duration_sec: float,
    phase_name: str = "",
    state: Optional[PhaseState] = None,
    shutdown_event: Optional[asyncio.Event] = None,
    global_semaphore: Optional[asyncio.Semaphore] = None,
    max_requests_per_worker: int = 30,
    rate_limit_delay: float = 0.1,
    rng: Optional[random.Random] = None,
    results_sink: Optional[List[Dict[str, Any]]] = None,
) -> List[Dict[str, Any]]:
    """Один воркер шлёт запросы, пока не истечёт время фазы.

    `global_semaphore` общий на всю фазу: `concurrency` — предел одновременных запросов
    фазы целиком, а не каждого воркера по отдельности.

    `results_sink` — список, в который замеры кладутся по мере готовности. Он же и
    возвращается. Вызывающий, который держит список у себя, читает собранное даже тогда,
    когда воркера пришлось снять принудительно.
    """
    state = state if state is not None else PhaseState()
    shutdown_event = shutdown_event if shutdown_event is not None else asyncio.Event()

    if global_semaphore is None:
        global_semaphore = asyncio.Semaphore(concurrency)
    local_semaphore = asyncio.Semaphore(max_requests_per_worker)
    payloads = endpoint_cfg.payloads or []
    results: List[Dict[str, Any]] = results_sink if results_sink is not None else []
    tasks: List[asyncio.Task] = []
    sent = 0
    start_monotonic = time.monotonic()

    def phase_over() -> bool:
        return shutdown_event.is_set() or time.monotonic() - start_monotonic >= phase_duration_sec

    async with create_http_client(
        timeout=endpoint_cfg.timeout,
        max_connections=concurrency + CONNECTION_HEADROOM,
        headers=endpoint_cfg.headers,
    ) as client:
        while not phase_over():
            await asyncio.sleep(next_request_delay(rate_limit_delay, rng))
            if phase_over():
                break
            await local_semaphore.acquire()
            if phase_over():
                local_semaphore.release()
                break

            variant = payloads[sent % len(payloads)] if payloads else None
            sent += 1
            tasks.append(asyncio.create_task(_tracked_request(
                client=client,
                endpoint_cfg=endpoint_cfg,
                payload=variant.body if variant is not None else None,
                global_semaphore=global_semaphore,
                local_semaphore=local_semaphore,
                state=state,
                phase_name=phase_name,
                worker_id=worker_id,
                sink=results,
            )))

        for item in await asyncio.gather(*tasks, return_exceptions=True):
            if isinstance(item, BaseException):
                log.warning("запрос воркера %s в фазе %s не дошёл: %s",
                            worker_id, phase_name, type(item).__name__)

    log.info("воркер %s, фаза %s: запросов %d, пик параллельности %d",
             worker_id, phase_name, len(results), state.peak)
    return results


async def launch_phase_workers(
    count: int,
    concurrency: int,
    endpoint_cfg: EndpointConfig,
    phase_duration_sec: float,
    phase_name: str = "",
    max_requests_per_worker: int = 30,
    rate_limit_delay: float = 0.1,
) -> List[Dict[str, Any]]:
    """Запускает воркеров фазы и собирает их результаты."""
    state = PhaseState()
    shutdown_event = asyncio.Event()
    # Семафор один на фазу: параллельность задана для фазы, а не для каждого воркера.
    global_semaphore = asyncio.Semaphore(concurrency)

    # Список на воркера заводит фаза, а не воркер: собранное должно оставаться у того, кто
    # переживёт отмену. `wait_for(gather(...))` этого не даёт — по истечении срока он
    # отменяет сам `gather`, тот отменяет всех детей, и замеры уходят вместе с ними.
    sinks: List[List[Dict[str, Any]]] = [[] for _ in range(count)]
    tasks = [
        asyncio.create_task(run_worker_phase(
            worker_id=f"w{i + 1}-{uuid.uuid4().hex[:8]}",
            concurrency=concurrency,
            endpoint_cfg=endpoint_cfg,
            phase_duration_sec=phase_duration_sec,
            phase_name=phase_name,
            state=state,
            shutdown_event=shutdown_event,
            global_semaphore=global_semaphore,
            max_requests_per_worker=max_requests_per_worker,
            rate_limit_delay=rate_limit_delay,
            results_sink=sinks[i],
        ))
        for i in range(count)
    ]

    _, pending = await asyncio.wait(tasks, timeout=phase_duration_sec + PHASE_DRAIN_SEC)
    overflowed = bool(pending)
    if pending:
        log.warning("фаза %s не уложилась в срок — просим воркеров остановиться", phase_name)
        shutdown_event.set()
        _, pending = await asyncio.wait(pending, timeout=PHASE_HARD_DRAIN_SEC)
        if pending:
            log.warning("фаза %s: снимаем задачи принудительно", phase_name)
            for task in pending:
                task.cancel()
            await asyncio.gather(*pending, return_exceptions=True)

    all_results: List[Dict[str, Any]] = [r for sink in sinks for r in sink]
    failures: List[BaseException] = []
    for task in tasks:
        if task.cancelled():
            continue
        exc = task.exception()
        if exc is None or isinstance(exc, asyncio.TimeoutError):
            continue
        failures.append(exc)
        log.warning("воркер фазы %s упал: %s", phase_name, exc)

    if not all_results and failures:
        raise PhaseFailedError(
            f"фаза {phase_name}: ни один из {count} воркеров не отправил запрос "
            f"({type(failures[0]).__name__}: {failures[0]})"
        ) from failures[0]

    if not all_results and overflowed:
        # Фаза, которая не уложилась в срок и не отдала ни одного замера, — это отказ, а не
        # пустой результат. Тихий вариант опаснее: в отчёт уходит «0 запросов, 0% ошибок»
        # ровно в тот момент, когда стенд просел, а лестница нагрузки едет выше.
        raise PhaseFailedError(
            f"фаза {phase_name}: ни один из {count} воркеров не успел отдать замер за "
            f"{phase_duration_sec + PHASE_DRAIN_SEC:.0f} с — все запросы остались в полёте"
        )

    log.info("фаза %s: воркеров %d, запросов %d, пик параллельности %d",
             phase_name, count, len(all_results), state.peak)
    return all_results
