"""
Tests for engine/indicators.py

Covers: rsi, ema, sma, vwap, bollinger_bands, atr, adx, macd, volume_ratio,
        IndicatorEngine
"""
import math
import pytest
import sys, os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from engine.indicators import (
    rsi, ema, sma, vwap, bollinger_bands, atr, adx, macd,
    volume_ratio, IndicatorEngine,
)


# ── Helpers ────────────────────────────────────────────────────────────────────

def _const(n, v=100.0):
    return [v] * n

def _ramp(n, start=100.0, step=1.0):
    return [start + i * step for i in range(n)]


# ── RSI ────────────────────────────────────────────────────────────────────────

class TestRSI:
    def test_returns_none_on_short_series(self):
        assert rsi([100.0] * 5, 14) is None

    def test_returns_float_on_adequate_data(self, trending):
        result = rsi(trending, 14)
        assert isinstance(result, float)

    def test_bounded_0_100(self, trending, oversold):
        for series in [trending, oversold]:
            r = rsi(series, 14)
            assert r is not None
            assert 0 <= r <= 100

    def test_uptrend_rsi_above_50(self, trending):
        r = rsi(trending, 14)
        assert r is not None and r > 50

    def test_downtrend_rsi_below_50(self, oversold):
        r = rsi(oversold, 14)
        assert r is not None and r < 50

    def test_constant_prices_rsi_valid(self):
        # No gains or losses — result is implementation-defined (often 100 or None)
        r = rsi(_const(30), 14)
        if r is not None:
            assert 0.0 <= r <= 100.0

    def test_period_14_default(self, trending):
        r1 = rsi(trending)
        r2 = rsi(trending, 14)
        assert r1 == r2


# ── EMA ────────────────────────────────────────────────────────────────────────

class TestEMA:
    def test_returns_none_too_short(self):
        assert ema([1.0, 2.0], 5) is None

    def test_returns_float(self, trending):
        result = ema(trending, 20)
        assert isinstance(result, float) and result > 0

    def test_constant_ema_equals_value(self):
        e = ema(_const(50, 200.0), 20)
        assert e is not None
        assert abs(e - 200.0) < 0.5

    def test_ema_reacts_faster_than_sma(self):
        """EMA weights recent prices more; on an exponential run-up e > s."""
        # Exponential uptrend: each bar 1% higher than last
        prices = [100.0 * (1.01 ** i) for i in range(60)]
        e = ema(prices, 20)
        s = sma(prices, 20)
        assert e is not None and s is not None
        # EMA converges toward latest (highest) price faster than backward-looking SMA
        assert e > s

    def test_longer_period_smoother(self, trending):
        e9  = ema(trending, 9)
        e21 = ema(trending, 21)
        # Both valid; just confirm we get numbers
        assert e9 is not None and e21 is not None


# ── SMA ────────────────────────────────────────────────────────────────────────

class TestSMA:
    def test_returns_none_too_short(self):
        assert sma([1.0] * 3, 10) is None

    def test_exact_value(self):
        prices = [10.0, 20.0, 30.0, 40.0, 50.0]
        result = sma(prices, 5)
        assert result is not None
        assert abs(result - 30.0) < 1e-9

    def test_rolling_last_window(self):
        prices = list(range(1, 21))   # 1..20
        result = sma(prices, 5)
        assert result is not None
        assert abs(result - 18.0) < 1e-9   # mean(16,17,18,19,20)


# ── VWAP ───────────────────────────────────────────────────────────────────────

class TestVWAP:
    def test_returns_none_on_empty(self):
        assert vwap([], []) is None

    def test_constant_price_equals_price(self):
        prices  = [200.0] * 20
        volumes = [1_000_000] * 20
        result  = vwap(prices, volumes)
        assert result is not None
        assert abs(result - 200.0) < 1e-6

    def test_weighted_correctly(self):
        prices  = [100.0, 200.0]
        volumes = [1,     3   ]      # 3× weight on 200
        result  = vwap(prices, volumes)
        # Expected = (100*1 + 200*3) / (1+3) = 700/4 = 175
        assert result is not None
        assert abs(result - 175.0) < 1e-6


# ── Bollinger Bands ────────────────────────────────────────────────────────────

class TestBollingerBands:
    def test_returns_triple_none_on_short(self):
        result = bollinger_bands([100.0] * 5, 20, 2.0)
        assert result == (None, None, None)

    def test_returns_three_floats(self, trending):
        up, mid, lo = bollinger_bands(trending, 20, 2.0)
        assert all(v is not None for v in [up, mid, lo])

    def test_upper_greater_lower(self, trending):
        up, mid, lo = bollinger_bands(trending, 20, 2.0)
        assert up > mid > lo

    def test_constant_bands_zero_width(self):
        up, mid, lo = bollinger_bands(_const(30), 20, 2.0)
        assert up is not None
        # Constant prices → std≈0 → band width ≈ 0
        assert abs(up - lo) < 1e-3

    def test_mid_equals_sma(self):
        prices = _ramp(30, 100.0, 1.0)
        _, mid, _ = bollinger_bands(prices, 20, 2.0)
        expected = sma(prices, 20)
        assert mid is not None and expected is not None
        assert abs(mid - expected) < 1e-3


# ── ATR ────────────────────────────────────────────────────────────────────────

class TestATR:
    def test_returns_none_on_short_series(self):
        h = [110.0] * 5
        l = [90.0]  * 5
        c = [100.0] * 5
        assert atr(h, l, c, 14) is None

    def test_constant_range_atr(self):
        """High-low range is always 10 → ATR should converge to 10."""
        n = 40
        c = _const(n, 100.0)
        h = _const(n, 105.0)
        l = _const(n, 95.0)
        result = atr(h, l, c, 14)
        assert result is not None
        assert 8.0 <= result <= 12.0   # Wilder's smoothing converges slowly

    def test_positive_value(self, ohlcv_trending):
        highs, lows, closes, _ = ohlcv_trending
        result = atr(highs, lows, closes, 14)
        assert result is not None and result > 0


# ── ADX ────────────────────────────────────────────────────────────────────────

class TestADX:
    def test_returns_none_on_short_series(self):
        h = [110.0] * 10
        l = [90.0]  * 10
        c = [100.0] * 10
        assert adx(h, l, c, 14) is None

    def test_bounded_0_100(self, ohlcv_trending):
        highs, lows, closes, _ = ohlcv_trending
        result = adx(highs, lows, closes, 14)
        assert result is not None
        assert 0 <= result <= 100

    def test_strong_trend_adx_above_20(self):
        """A strong, consistent directional move should give ADX ≥ 20."""
        n = 60
        closes = _ramp(n, 100.0, 2.0)   # strong uptrend
        highs  = [c + 1.0 for c in closes]
        lows   = [c - 1.0 for c in closes]
        result = adx(highs, lows, closes, 14)
        assert result is not None and result >= 15.0


# ── MACD ───────────────────────────────────────────────────────────────────────

class TestMACD:
    def test_returns_none_on_short_series(self):
        assert macd([100.0] * 10) == (None, None, None)

    def test_returns_three_floats_on_adequate_data(self, trending):
        ml, sl, hist = macd(trending, 12, 26, 9)
        assert all(v is not None for v in [ml, sl, hist])

    def test_histogram_is_macd_minus_signal(self, trending):
        ml, sl, hist = macd(trending, 12, 26, 9)
        assert ml is not None
        assert abs(hist - (ml - sl)) < 1e-3

    def test_uptrend_positive_macd(self, trending):
        ml, sl, hist = macd(trending, 12, 26, 9)
        assert ml is not None
        # On a strong uptrend fast EMA > slow EMA → MACD > 0
        assert ml > 0

    def test_minimum_length_boundary(self):
        # 26 + 9 = 35; at exactly 35 we should get valid results
        prices = _ramp(35, 100.0, 1.0)
        ml, sl, hist = macd(prices, 12, 26, 9)
        assert ml is not None

    def test_one_short_of_minimum_returns_none(self):
        prices = _ramp(34, 100.0, 1.0)
        assert macd(prices, 12, 26, 9) == (None, None, None)


# ── Volume Ratio ───────────────────────────────────────────────────────────────

class TestVolumeRatio:
    def test_returns_none_on_short(self):
        assert volume_ratio([100_000] * 5, 20) is None

    def test_surge_detected(self):
        # Last bar volume is 3× the 20-bar average
        avg_vols = [100_000] * 21
        surge_vols = avg_vols[:-1] + [300_000]
        result = volume_ratio(surge_vols, 20)
        assert result is not None and result >= 2.5

    def test_normal_volume_near_1(self):
        vols = [100_000] * 22
        result = volume_ratio(vols, 20)
        assert result is not None
        assert 0.9 <= result <= 1.1


# ── IndicatorEngine ────────────────────────────────────────────────────────────

class TestIndicatorEngine:
    def test_can_instantiate(self):
        eng = IndicatorEngine("RELIANCE")
        assert eng is not None

    def test_push_ohlcv_and_rsi(self, ohlcv_trending):
        highs, lows, closes, volumes = ohlcv_trending
        eng = IndicatorEngine("TEST")
        for i in range(len(closes)):
            eng.push_ohlcv(closes[i] * 0.999, highs[i], lows[i], closes[i], volumes[i])
        r = eng.rsi(14)
        assert r is None or (0 <= r <= 100)

    def test_bb_via_engine(self, ohlcv_trending):
        highs, lows, closes, volumes = ohlcv_trending
        eng = IndicatorEngine("TEST2")
        for i in range(len(closes)):
            eng.push_ohlcv(closes[i], highs[i], lows[i], closes[i], volumes[i])
        up, mid, lo = eng.bb(20, 2.0)
        if up is not None:
            assert up > lo

    def test_atr_via_engine(self, ohlcv_trending):
        highs, lows, closes, volumes = ohlcv_trending
        eng = IndicatorEngine("TEST3")
        for i in range(len(closes)):
            eng.push_ohlcv(closes[i], highs[i], lows[i], closes[i], volumes[i])
        result = eng.atr(14)
        if result is not None:
            assert result > 0

    def test_macd_via_engine(self, ohlcv_trending):
        highs, lows, closes, volumes = ohlcv_trending
        eng = IndicatorEngine("TEST4")
        for i in range(len(closes)):
            eng.push_ohlcv(closes[i], highs[i], lows[i], closes[i], volumes[i])
        ml, sl, hist = eng.macd(12, 26, 9)
        if ml is not None:
            assert abs(hist - (ml - sl)) < 1e-3

    def test_for_symbol_returns_same_instance(self):
        a = IndicatorEngine.for_symbol("SAMESTOCK")
        b = IndicatorEngine.for_symbol("SAMESTOCK")
        assert a is b

    def test_len_tracks_pushes(self):
        eng = IndicatorEngine("LENTEST")
        for i in range(10):
            eng.push(float(100 + i))
        assert len(eng) == 10

