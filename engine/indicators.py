"""
Technical Indicators — standalone module.
Pure functions + per-symbol rolling IndicatorEngine.
"""

import math
import threading
from collections import deque
from typing import Optional


# ── Pure functions ─────────────────────────────────────────────────────────────

def rsi(prices: list, period: int = 14) -> Optional[float]:
    if len(prices) < period + 1:
        return None
    window = list(prices)[-(period + 1):]
    gains  = [max(window[i] - window[i-1], 0.0) for i in range(1, len(window))]
    losses = [max(window[i-1] - window[i], 0.0) for i in range(1, len(window))]
    avg_g  = sum(gains)  / period
    avg_l  = sum(losses) / period
    if avg_l == 0:
        return 100.0
    return round(100.0 - 100.0 / (1.0 + avg_g / avg_l), 2)


def ema(prices: list, period: int) -> Optional[float]:
    if len(prices) < period:
        return None
    p   = list(prices)
    k   = 2.0 / (period + 1)
    val = sum(p[:period]) / period
    for px in p[period:]:
        val = px * k + val * (1.0 - k)
    return round(val, 2)


def sma(prices: list, period: int) -> Optional[float]:
    if len(prices) < period:
        return None
    return round(sum(list(prices)[-period:]) / period, 2)


def vwap(prices: list, volumes: list = None) -> Optional[float]:
    if not prices:
        return None
    p = list(prices)
    if volumes and len(volumes) == len(p):
        v = list(volumes)
        total = sum(v)
        if total > 0:
            return round(sum(px * vl for px, vl in zip(p, v)) / total, 2)
    return round(sum(p) / len(p), 2)


def bollinger_bands(prices: list, period: int = 20,
                    std_dev: float = 2.0) -> tuple:
    if len(prices) < period:
        return None, None, None
    recent = list(prices)[-period:]
    mid    = sum(recent) / period
    std    = math.sqrt(sum((p - mid) ** 2 for p in recent) / period)
    return round(mid + std_dev * std, 2), round(mid, 2), round(mid - std_dev * std, 2)


# ── Per-symbol rolling engine ──────────────────────────────────────────────────

class IndicatorEngine:
    _instances: dict = {}
    _lock = threading.Lock()

    @classmethod
    def for_symbol(cls, symbol: str, maxlen: int = 500) -> "IndicatorEngine":
        with cls._lock:
            if symbol not in cls._instances:
                cls._instances[symbol] = cls(symbol, maxlen)
            return cls._instances[symbol]

    def __init__(self, symbol: str, maxlen: int = 500):
        self.symbol   = symbol
        self._prices  = deque(maxlen=maxlen)
        self._volumes = deque(maxlen=maxlen)
        self._lock    = threading.Lock()

    def push(self, price: float, volume: float = 0.0) -> None:
        with self._lock:
            self._prices.append(float(price))
            self._volumes.append(float(volume))

    def __len__(self) -> int:
        with self._lock:
            return len(self._prices)

    def _p(self) -> list:
        with self._lock:
            return list(self._prices)

    def _v(self) -> list:
        with self._lock:
            return list(self._volumes)

    def rsi(self, period: int = 14) -> Optional[float]:    return rsi(self._p(), period)
    def ema(self, period: int)       -> Optional[float]:    return ema(self._p(), period)
    def sma(self, period: int)       -> Optional[float]:    return sma(self._p(), period)
    def vwap(self)                   -> Optional[float]:    return vwap(self._p(), self._v())
    def bb(self, period: int = 20, std_dev: float = 2.0) -> tuple:
        return bollinger_bands(self._p(), period, std_dev)
