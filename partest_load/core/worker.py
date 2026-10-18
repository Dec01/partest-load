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
from datetime import datetime, timezone
from typing import Any, Callable, Dict, List, Optional

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

# Как часто фаза сверяет свежие замеры с порогами остановки профиля, если они заданы.
_STOP_CHECK_INTERVAL_SEC = 1.0

# Проверка порогов остановки: свежие замеры → причины остановиться (пусто — продолжаем).
_StopCheck = Callable[[List[Dict[str, Any]]], List[str]]


def _finished_at(result: Dict[str, Any]) -> float:
    """Когда запрос завершился: начало плюс длительность.

    По этому моменту строится окно порогов остановки. По началу запроса строить нельзя:
    медленный или повисший запрос начинается рано, а завершается (часто таймаутом) поздно,
    и в окно «последних по началу» он не попадал бы никогда — именно тогда, когда система
    перестала держать.
    """
    return (result.get("request_start_ts") or 0.0) + (result.get("duration_sec") or 0.0)


def _latest_finished(results, window: int) -> List[Dict[str, Any]]:
    """До `window` последних завершившихся замеров, в порядке завершения."""
    return sorted(results, key=_finished_at)[-window:]


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


class _RequestBudget:
    """Конечный список запросов фазы, общий на всех её воркеров.

    `take()` отдаёт номер следующего запроса списка или `None`, если список пройден. Номер
    общий, а не свой у каждого воркера: варианты ротируются по нему, и каждый вариант
    отправляется ровно столько раз, сколько заказано, сколько бы воркеров ни было.
    Блокировка не нужна — воркеры фазы живут в одном цикле событий.
    """

    def __init__(self, total: int) -> None:
        if total < 0:
            raise ValueError("размер списка запросов не может быть отрицательным")
        self.total = total
        self.taken = 0

    def take(self) -> Optional[int]:
        if self.taken >= self.total:
            return None
        index = self.taken
        self.taken += 1
        return index


def _cancelled_result(endpoint_cfg: EndpointConfig, payload, path_params, started: float,
                      worker_id: str, phase_name: str, active: int) -> Dict[str, Any]:
    """Замер запроса, снятого отменой: неуспех, `error_type: cancelled`, время до отмены.

    Длительность — нижняя оценка: запрос не вернулся, известно только, что он шёл не меньше.
    """
    return {
        "timestamp": datetime.fromtimestamp(started, timezone.utc).isoformat(),
        "request_start_ts": started,
        "url": endpoint_cfg.url(path_params),
        "method": endpoint_cfg.method.upper(),
        "payload": payload,
        "success": False,
        "duration_sec": round(max(0.0, time.time() - started), 4),
        "size_mb": 0.0,
        "status": 0,
        "error": "cancelled at the phase boundary",
        "error_type": "cancelled",
        "worker_id": worker_id,
        "phase": phase_name,
        "active_concurrency": active,
    }


async def _tracked_request(
    client,
    endpoint_cfg: EndpointConfig,
    payload: Optional[Dict[str, Any]],
    path_params: Optional[Dict[str, Any]],
    global_semaphore: asyncio.Semaphore,
    local_semaphore: asyncio.Semaphore,
    state: PhaseState,
    phase_name: str,
    worker_id: str,
    sink: List[Dict[str, Any]],
    phase_over: Optional[Callable[[], bool]] = None,
) -> Optional[Dict[str, Any]]:
    """Один запрос фазы. `None` — запрос не ушёл: фаза кончилась, пока он ждал очереди.

    Задача создаётся раньше, чем запрос получает место под потолком фазы, и ждёт его в
    очереди семафора. Получив место после конца фазы (время вышло или сработал порог), она
    ничего не отправляет и замера не пишет: нагрузка фазы кончается вместе с фазой.
    """
    try:
        async with global_semaphore:
            if phase_over is not None and phase_over():
                return None
            current_active = state.enter()
            req_start_ts = time.time()
            state.request_starts.append(req_start_ts)
            try:
                try:
                    result = await make_single_request(
                        client=client,
                        endpoint_cfg=endpoint_cfg,
                        payload=payload,
                        path_params=path_params,
                    )
                except asyncio.CancelledError:
                    # Запрос, снятый на выдержке фазы, — тоже замер: он ушёл и не вернулся.
                    # Без этой записи частично повисший стенд давал 0 % ошибок.
                    sink.append(_cancelled_result(endpoint_cfg, payload, path_params,
                                                  req_start_ts, worker_id, phase_name,
                                                  current_active))
                    raise
                result.update({
                    "worker_id": worker_id,
                    "phase": phase_name,
                    "active_concurrency": current_active,
                })
                # Замер складывается в общий список сразу, а не возвращается наружу через
                # значение задачи: всё, что живёт только в возвращаемом значении отменённой
                # задачи, теряется вместе с ней.
                sink.append(result)
                return result
            finally:
                state.leave()
    finally:
        # Местный семафор держится всё время запроса, а не только на создании задачи:
        # иначе max_requests_per_worker не ограничивал бы ничего.
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
    budget: Optional[_RequestBudget] = None,
) -> List[Dict[str, Any]]:
    """Один воркер шлёт запросы, пока не истечёт время фазы.

    `global_semaphore` общий на всю фазу: `concurrency` — предел одновременных запросов
    фазы целиком, а не каждого воркера по отдельности.

    `results_sink` — список, в который замеры кладутся по мере готовности. Он же и
    возвращается. Вызывающий, который держит список у себя, читает собранное даже тогда,
    когда воркера пришлось снять принудительно.

    `budget` — конечный список запросов фазы. С ним воркер заканчивает, когда список
    пройден, даже если время фазы ещё есть; время остаётся пределом.
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
        auth=endpoint_cfg.basic_auth(),
    ) as client:
        try:
            while not phase_over():
                await asyncio.sleep(next_request_delay(rate_limit_delay, rng))
                if phase_over():
                    break
                await local_semaphore.acquire()
                if phase_over():
                    local_semaphore.release()
                    break

                if budget is not None:
                    index = budget.take()
                    if index is None:
                        local_semaphore.release()
                        break
                else:
                    index = sent
                variant = payloads[index % len(payloads)] if payloads else None
                sent += 1
                tasks.append(asyncio.create_task(_tracked_request(
                    client=client,
                    endpoint_cfg=endpoint_cfg,
                    payload=variant.body if variant is not None else None,
                    path_params=variant.path_params if variant is not None else None,
                    global_semaphore=global_semaphore,
                    local_semaphore=local_semaphore,
                    state=state,
                    phase_name=phase_name,
                    worker_id=worker_id,
                    sink=results,
                    phase_over=phase_over,
                )))

            for item in await asyncio.gather(*tasks, return_exceptions=True):
                if isinstance(item, BaseException):
                    log.warning("запрос воркера %s в фазе %s не дошёл: %s",
                                worker_id, phase_name, type(item).__name__)
        finally:
            # Снятый воркер (отмена на выдержке фазы) снимает и свои запросы. Иначе
            # они оставались жить после фазы: ждавшие семафора уходили на стенд уже
            # после её конца, а их замеры ложились в список, который фаза уже прочла.
            unfinished = [task for task in tasks if not task.done()]
            for task in unfinished:
                task.cancel()
            if unfinished:
                await asyncio.gather(*unfinished, return_exceptions=True)

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
    request_budget: Optional[int] = None,
    stop_check: Optional[_StopCheck] = None,
    stop_window: int = 300,
) -> List[Dict[str, Any]]:
    """Запускает воркеров фазы и собирает их результаты.

    `request_budget` — размер конечного списка запросов фазы (профиль batch): фаза
    кончается, когда список пройден или вышло время.

    `stop_check` — пороги остановки профиля. Раз в `_STOP_CHECK_INTERVAL_SEC` ему дают до
    `stop_window` самых свежих замеров фазы; вернул причины — воркеры перестают отправлять,
    уже отправленное дожидается. Проверка внутри фазы, а не только на её границе: у linear
    без разгона фаза одна на весь прогон, и порог на границе сработал бы, когда уже поздно.
    """
    state = PhaseState()
    shutdown_event = asyncio.Event()
    # Семафор один на фазу: параллельность задана для фазы, а не для каждого воркера.
    global_semaphore = asyncio.Semaphore(concurrency)
    budget = _RequestBudget(request_budget) if request_budget is not None else None

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
            budget=budget,
        ))
        for i in range(count)
    ]

    async def watch() -> None:
        while not shutdown_event.is_set():
            await asyncio.sleep(_STOP_CHECK_INTERVAL_SEC)
            # Хвост каждого воркера, а не всё собранное: на длинной фазе это сотни тысяч
            # замеров, и сортировать их раз в секунду значило бы отнимать время у нагрузки.
            # Замер ложится в список воркера по завершении, так что хвост — последние
            # завершившиеся, включая медленные запросы, начатые давно.
            tail = _latest_finished((r for sink in sinks for r in sink[-stop_window:]),
                                   stop_window)
            reasons = stop_check(tail)
            if reasons:
                log.warning("фаза %s остановлена по порогу профиля: %s",
                            phase_name, ", ".join(reasons))
                shutdown_event.set()
                return

    watcher = asyncio.create_task(watch()) if stop_check is not None else None

    try:
        _, pending = await asyncio.wait(tasks, timeout=phase_duration_sec + PHASE_DRAIN_SEC)
        overflowed = bool(pending)
        if pending:
            log.warning("фаза %s не уложилась в срок — просим воркеров остановиться",
                        phase_name)
            shutdown_event.set()
            _, pending = await asyncio.wait(pending, timeout=PHASE_HARD_DRAIN_SEC)
            if pending:
                log.warning("фаза %s: снимаем задачи принудительно", phase_name)
                for task in pending:
                    task.cancel()
                await asyncio.gather(*pending, return_exceptions=True)
    finally:
        # И на штатном пути, и при отмене снаружи (`wait_for`, Ctrl+C): ни воркеры, ни
        # наблюдатель порогов фазу не переживают.
        shutdown_event.set()
        leftovers = [task for task in tasks if not task.done()]
        if watcher is not None:
            leftovers.append(watcher)
        for task in leftovers:
            task.cancel()
        if leftovers:
            await asyncio.gather(*leftovers, return_exceptions=True)

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

    if overflowed and all(r.get("error_type") == "cancelled" for r in all_results):
        # Фаза, которая не уложилась в срок и не отдала ни одного ответа (только снятые
        # отменой запросы или ничего), — это отказ, а не пустой результат. Тихий вариант опаснее: в отчёт уходит «0 запросов, 0% ошибок»
        # ровно в тот момент, когда стенд просел, а лестница нагрузки едет выше.
        raise PhaseFailedError(
            f"фаза {phase_name}: ни один из {count} воркеров не успел отдать замер за "
            f"{phase_duration_sec + PHASE_DRAIN_SEC:.0f} с — все запросы остались в полёте"
        )

    log.info("фаза %s: воркеров %d, запросов %d, пик параллельности %d",
             phase_name, count, len(all_results), state.peak)
    return all_results
