"""Асинхронный HTTP-клиент и один запрос.

Библиотека ничего не печатает: то, что в прототипе уходило в stdout (`Non-2xx`, `DEBUG:`),
здесь идёт в логгер. Печать из библиотеки нельзя ни выключить, ни перенаправить, а под
нагрузкой она сама становится тормозом.
"""

from __future__ import annotations

import logging
import time
from datetime import datetime, timezone
from typing import Any, Dict, Optional

import httpx

from ..configs.endpoints import BODY_METHODS, EndpointConfig

log = logging.getLogger(__name__)

# Границы диапазонов размера ответа. Нужны отчёту: деградация на крупных ответах
# выглядит иначе, чем на мелких.
SIZE_RANGES = (
    (0.0, 1.0, "0–1 MB"),
    (1.0, 3.0, "1–3 MB"),
    (3.0, 5.0, "3–5 MB"),
    (5.0, 10.0, "5–10 MB"),
    (10.0, float("inf"), "10+ MB"),
)


def create_http_client(
    timeout: float = 15.0,
    max_connections: int = 200,
    http2: bool = True,
    headers: Optional[Dict[str, str]] = None,
    verify: bool = True,
) -> httpx.AsyncClient:
    """Клиент с лимитами пула и обязательным предельным временем."""
    limits = httpx.Limits(
        max_connections=max_connections,
        max_keepalive_connections=max_connections // 2,
        keepalive_expiry=60.0,
    )
    default_headers = {
        "User-Agent": "partest-load/1.0",
        "Accept": "application/json",
        "Accept-Encoding": "gzip, deflate, br",
    }
    if headers:
        default_headers.update(headers)

    return httpx.AsyncClient(
        http2=http2,
        timeout=httpx.Timeout(timeout, connect=timeout / 2),
        limits=limits,
        headers=default_headers,
        follow_redirects=True,
        # Проверка сертификата остаётся включённой. Стенду с самоподписанным
        # сертификатом это выключает потребитель — осознанно и у себя.
        verify=verify,
    )


def size_range(size_mb: float) -> str:
    for low, high, label in SIZE_RANGES:
        if low <= size_mb < high:
            return label
    return "unknown"


def classify_status(status: int) -> str:
    """Тип ошибки по коду ответа. Имена типов уходят в отчёт и в артефакт."""
    if status == 429:
        return "rate_limit_429"
    if 500 <= status < 600:
        return "server_5xx"
    return f"http_{status}"


async def make_single_request(
    client: httpx.AsyncClient,
    endpoint_cfg: EndpointConfig,
    payload: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """Один запрос. Не бросает исключений: любая ошибка — это тоже результат замера."""
    url = endpoint_cfg.url()
    # Вариант с готовым адресом: профиль перебирает конечный список ссылок, а не
    # собирает адрес из базы и пути.
    if payload and "full_url" in payload:
        url = payload["full_url"]

    method = endpoint_cfg.method.upper()
    timeout = endpoint_cfg.timeout

    start_perf = time.perf_counter()
    result: Dict[str, Any] = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "request_start_ts": time.time(),
        "url": url,
        "method": method,
        "payload": payload,
        "success": False,
        "duration_sec": 0.0,
        "size_mb": 0.0,
        "status": 0,
        "error": "",
        "error_type": None,
    }

    try:
        resp = await client.request(
            method,
            url,
            json=payload if method in BODY_METHODS else None,
            params=endpoint_cfg.query_params,
            headers=endpoint_cfg.headers or None,
            timeout=timeout,
        )
        duration = time.perf_counter() - start_perf
        size_mb = len(resp.content) / (1024 * 1024)
        result.update({
            "status": resp.status_code,
            "duration_sec": round(duration, 4),
            "size_mb": round(size_mb, 4),
            "size_range": size_range(size_mb),
            "success": 200 <= resp.status_code < 300,
        })
        if not result["success"]:
            # Тело ответа обрезается: в отчёт попадает признак, а не чужие данные.
            result["error"] = resp.text[:500]
            result["error_type"] = classify_status(resp.status_code)
            log.debug("не-2xx %s %s", resp.status_code, url)

    except httpx.TimeoutException:
        result["duration_sec"] = round(time.perf_counter() - start_perf, 4)
        result["error"] = "Request timeout"
        result["error_type"] = "timeout"
    except httpx.RequestError as exc:
        result["duration_sec"] = round(time.perf_counter() - start_perf, 4)
        result["error"] = str(exc)[:500]
        result["error_type"] = "request_error"
    except Exception as exc:  # noqa: BLE001 — падение одного запроса не роняет прогон
        result["duration_sec"] = round(time.perf_counter() - start_perf, 4)
        result["error"] = f"Unexpected: {type(exc).__name__} {exc}"[:500]
        result["error_type"] = "unexpected_exception"
        log.warning("неожиданная ошибка запроса %s: %s", url, type(exc).__name__)

    return result


def payload_label(payload: Optional[Dict[str, Any]]) -> str:
    """Краткая метка варианта запроса для отчёта.

    Ключи берутся из самого тела и в пакете не перечисляются: какие поля осмысленны,
    знает только тот, чей это контракт.
    """
    if payload is None:
        return "без тела"
    if not isinstance(payload, dict):
        return "тело не объект"
    parts = [f"{key}={str(value)[:16]}" for key, value in list(payload.items())[:3]
             if not isinstance(value, (dict, list))]
    return " | ".join(parts) or "объект без скалярных полей"
