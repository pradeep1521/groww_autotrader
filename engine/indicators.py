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


def atr(highs: list, lows: list, closes: list, period: int = 14) -> Optional[float]:
    """Average True Range — Wilder's smoothing."""
    if len(closes) < period + 1:
        return None
    trs = []
    for i in range(1, len(closes)):
        tr = max(
            highs[i]  - lows[i],
            abs(highs[i]  - closes[i - 1]),
            abs(lows[i]   - closes[i - 1]),
        )
        trs.append(tr)
    if len(trs) < period:
        return None
    val = sum(trs[:period]) / period
    for tr in trs[period:]:
        val = (val * (period - 1) + tr) / period
    return round(val, 4)


def adx(highs: list, lows: list, closes: list, period: int = 14) -> Optional[float]:
    """Average Directional Index — trend strength 0-100.  >25 = trending."""
    if len(closes) < period * 2 + 1:
        return None
    dm_plus, dm_minus, trs = [], [], []
    for i in range(1, len(closes)):
        up   = highs[i]  - highs[i - 1]
        down = lows[i - 1] - lows[i]
        dm_plus.append(up   if up > down and up > 0 else 0.0)
        dm_minus.append(down if down > up and down > 0 else 0.0)
        tr = max(highs[i] - lows[i],
                 abs(highs[i] - closes[i - 1]),
                 abs(lows[i]  - closes[i - 1]))
        trs.append(tr)
    # Wilder smoothing
    def _smooth(lst):
        val = sum(lst[:period]) / period
        res = [val]
        for x in lst[period:]:
            val = (val * (period - 1) + x) / period
            res.append(val)
        return res
    atr_s  = _smooth(trs)
    dmp_s  = _smooth(dm_plus)
    dmn_s  = _smooth(dm_minus)
    dx_lst = []
    for a, p, n in zip(atr_s, dmp_s, dmn_s):
        if a == 0:
            continue
        di_plus  = 100 * p / a
        di_minus = 100 * n / a
        denom    = di_plus + di_minus
        dx_lst.append(100 * abs(di_plus - di_minus) / denom if denom else 0)
    if not dx_lst:
        return None
    adx_val = sum(dx_lst[-period:]) / min(period, len(dx_lst))
    return round(adx_val, 2)


def volume_ratio(volumes: list, period: int = 20) -> Optional[float]:
    """Current volume vs N-period average.  >1.5 = surge."""
    if len(volumes) < period + 1:
        return None
    avg = sum(list(volumes)[-(period + 1):-1]) / period
    return round(list(volumes)[-1] / avg, 2) if avg > 0 else 1.0


def macd(prices: list, fast: int = 12, slow: int = 26,
         signal_period: int = 9) -> tuple:
    """Returns (macd_line, signal_line, histogram) or (None, None, None).

    Uses a single O(n) pass — each EMA is computed incrementally rather
    than re-seeding from scratch for every bar (avoids the previous O(n²) loop).
    """
    p = list(prices)
    if len(p) < slow + signal_period:
        return None, None, None

    k_fast = 2.0 / (fast + 1)
    k_slow = 2.0 / (slow + 1)

    # Seed both EMAs at the SMA of their first `period` bars
    ema_fast = sum(p[:fast]) / fast
    ema_slow = sum(p[:slow]) / slow

    # Advance fast EMA to bar `slow - 1` (so both are aligned from bar `slow`)
    for px in p[fast:slow]:
        ema_fast = px * k_fast + ema_fast * (1 - k_fast)

    # Build MACD line history from bar `slow` onward
    macd_series: list = []
    for px in p[slow:]:
        ema_fast = px * k_fast + ema_fast * (1 - k_fast)
        ema_slow = px * k_slow + ema_slow * (1 - k_slow)
        macd_series.append(ema_fast - ema_slow)

    if len(macd_series) < signal_period:
        return None, None, None

    # Signal line = EMA(signal_period) of MACD series
    k_sig    = 2.0 / (signal_period + 1)
    sig_val  = sum(macd_series[:signal_period]) / signal_period
    for v in macd_series[signal_period:]:
        sig_val = v * k_sig + sig_val * (1 - k_sig)

    macd_line = macd_series[-1]
    histogram = round(macd_line - sig_val, 4)
    return round(macd_line, 4), round(sig_val, 4), histogram


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
        self._highs   = deque(maxlen=maxlen)
        self._lows    = deque(maxlen=maxlen)
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

    def rsi(self, period: int = 14)   -> Optional[float]: return rsi(self._p(), period)
    def ema(self, period: int)        -> Optional[float]: return ema(self._p(), period)
    def sma(self, period: int)        -> Optional[float]: return sma(self._p(), period)
    def vwap(self)                    -> Optional[float]: return vwap(self._p(), self._v())
    def volume_ratio(self, period: int = 20) -> Optional[float]:
        return volume_ratio(self._v(), period)
    def bb(self, period: int = 20, std_dev: float = 2.0) -> tuple:
        return bollinger_bands(self._p(), period, std_dev)
    def macd(self, fast: int = 12, slow: int = 26, signal: int = 9) -> tuple:
        return macd(self._p(), fast, slow, signal)

    # ── Requires OHLC data ─────────────────────────────────────────────────────

    def push_ohlcv(self, open_: float, high: float, low: float,
                   close: float, volume: float = 0.0) -> None:
        """Push a full OHLCV bar (stores close in price stream, full bar in ohlcv)."""
        with self._lock:
            self._prices.append(float(close))
            self._volumes.append(float(volume))
            self._highs.append(float(high))
            self._lows.append(float(low))

    def atr(self, period: int = 14) -> Optional[float]:
        with self._lock:
            h, l, c = list(self._highs), list(self._lows), list(self._prices)
        return atr(h, l, c, period)

    def adx(self, period: int = 14) -> Optional[float]:
        with self._lock:
            h, l, c = list(self._highs), list(self._lows), list(self._prices)
        return adx(h, l, c, period)
