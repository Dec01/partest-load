"""Момент следующего запроса.

В этой функции вся суть движка, и сторожить в ней надо не «вызывается ли random», а два
свойства, от которых зависит, какую нагрузку инструмент создаёт на самом деле: разброс
несимметричен в безопасную сторону и пауза никогда не отрицательна.
"""

from __future__ import annotations

import random

import pytest

from partest_load.core.pacing import JITTER_MAX, JITTER_MIN, next_request_delay


def test_jitter_is_asymmetric_toward_longer_pause():
    """В среднем пауза длиннее базовой, а не равна ей.

    Это решение, а не случайность: инструмент скорее недодаст нагрузки, чем перегрузит
    чужой стенд. Симметричный разброс (−0.5…+0.5) прошёл бы любой тест на «пауза в
    допустимых границах» и молча увеличил бы нагрузку на 10%.
    """
    rng = random.Random(20260912)
    base = 0.1
    samples = [next_request_delay(base, rng) for _ in range(20_000)]
    average = sum(samples) / len(samples)

    assert average == pytest.approx(base * 1.1, rel=0.02)
    assert average > base


def test_delay_stays_within_declared_bounds():
    """Крайние значения разброса дают ровно объявленные границы паузы."""

    class Edge(random.Random):
        def __init__(self, value: float) -> None:
            super().__init__()
            self.value = value

        def uniform(self, a: float, b: float) -> float:  # noqa: D102
            assert (a, b) == (JITTER_MIN, JITTER_MAX)
            return self.value

    assert next_request_delay(0.2, Edge(JITTER_MIN)) == pytest.approx(0.2 * 0.6)
    assert next_request_delay(0.2, Edge(JITTER_MAX)) == pytest.approx(0.2 * 1.6)


def test_delay_never_negative_even_if_jitter_goes_below_minus_one():
    """Отрицательная пауза — это `asyncio.sleep(-x)`, то есть запрос без паузы вообще."""

    class TooNegative(random.Random):
        def uniform(self, a: float, b: float) -> float:  # noqa: D102
            return -5.0

    assert next_request_delay(0.3, TooNegative()) == 0.0


def test_zero_rate_limit_gives_zero_pause():
    """Нулевая базовая пауза остаётся нулём при любом разбросе: умножение, а не сложение."""
    rng = random.Random(1)
    assert all(next_request_delay(0.0, rng) == 0.0 for _ in range(50))


def test_negative_rate_limit_is_refused():
    """Отрицательная базовая пауза — ошибка в профиле, и о ней надо узнать сразу."""
    with pytest.raises(ValueError):
        next_request_delay(-0.1)
