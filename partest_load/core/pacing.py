"""Момент следующего запроса.

Здесь одна функция, и в ней вся суть движка: сколько воркер ждёт перед очередной
попыткой отправки. Базовая пауза приходит из профиля (`1 / rate_limit_rps`), а разброс
нужен, чтобы воркеры не выстраивались в такт и не били по системе пачками — ровный
пилообразный трафик ничего не говорит о том, как система держит настоящую нагрузку.

Разброс несимметричный: в среднем пауза на 10% длиннее базовой. Так задумано — ошибка
в безопасную сторону, инструмент скорее недодаст нагрузки, чем перегрузит чужой стенд.
"""

from __future__ import annotations

import random
from typing import Optional

# Границы разброса как доля от базовой паузы: от −40% до +60%.
JITTER_MIN = -0.4
JITTER_MAX = 0.6


def next_request_delay(
    rate_limit_delay: float,
    rng: Optional[random.Random] = None,
) -> float:
    """Пауза до следующей попытки отправки, в секундах.

    `rng` принимается явно, чтобы паузу можно было проверить, а не наблюдать.
    """
    if rate_limit_delay < 0:
        raise ValueError("rate_limit_delay не может быть отрицательным")
    source = rng if rng is not None else random
    jitter = source.uniform(JITTER_MIN, JITTER_MAX)
    return max(0.0, rate_limit_delay * (1.0 + jitter))
