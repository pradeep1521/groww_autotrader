"""
Shared fixtures for groww_autotrader tests.
"""
import random
import math
import pytest


def _trending_prices(n: int = 100, start: float = 1000.0, drift: float = 0.003) -> list:
    """Steady uptrend with small noise."""
    rng = random.Random(42)
    p = start
    out = []
    for _ in range(n):
        p *= 1 + drift + rng.gauss(0, 0.005)
        out.append(round(p, 2))
    return out


def _flat_prices(n: int = 100, base: float = 500.0) -> list:
    """Nearly flat prices — tiny random walk."""
    rng = random.Random(7)
    return [round(base + rng.gauss(0, 0.1), 2) for _ in range(n)]


def _oversold_prices(n: int = 100, start: float = 1000.0) -> list:
    """Sharp downtrend to push RSI into oversold territory."""
    rng = random.Random(13)
    p = start
    out = []
    for _ in range(n):
        p *= 0.997 + rng.gauss(0, 0.003)
        out.append(max(1.0, round(p, 2)))
    return out


def _make_ohlcv(closes: list) -> tuple:
    """Create plausible high/low/volume arrays matching a closes list."""
    rng = random.Random(99)
    highs   = [c * (1 + rng.uniform(0.001, 0.01)) for c in closes]
    lows    = [c * (1 - rng.uniform(0.001, 0.01)) for c in closes]
    volumes = [int(rng.uniform(5_00_000, 20_00_000)) for _ in closes]
    return highs, lows, volumes


@pytest.fixture
def trending():
    return _trending_prices(100)


@pytest.fixture
def flat():
    return _flat_prices(100)


@pytest.fixture
def oversold():
    return _oversold_prices(100)


@pytest.fixture
def ohlcv_trending():
    closes = _trending_prices(100)
    highs, lows, volumes = _make_ohlcv(closes)
    return highs, lows, closes, volumes
