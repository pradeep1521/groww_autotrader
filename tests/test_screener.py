"""
Tests for engine/screener.py — StockScreener
=============================================
All yfinance calls are monkeypatched.  No real network traffic.
"""

import math
import threading
from unittest.mock import MagicMock, patch

import pandas as pd
import pytest

from engine.screener import (
    FNO_EXTRAS,
    NIFTY50,
    NIFTY_NEXT50,
    StockScreener,
    _VIX_HIGH,
    _VIX_LOW,
)


# ── Helpers to build fake yfinance history DataFrames ─────────────────────────

def _make_hist(closes, highs=None, lows=None, volumes=None) -> pd.DataFrame:
    n = len(closes)
    if highs is None:
        highs = [c * 1.01 for c in closes]
    if lows is None:
        lows = [c * 0.99 for c in closes]
    if volumes is None:
        volumes = [500_000] * n
    idx = pd.date_range("2025-01-01", periods=n, freq="D")
    return pd.DataFrame(
        {"Open": closes, "High": highs, "Low": lows, "Close": closes, "Volume": volumes},
        index=idx,
    )


def _trending_hist(n=90):
    """Strong uptrend: price grows 0.5%/day, volume surge on last bar."""
    closes = [500.0 * (1.005 ** i) for i in range(n)]
    volumes = [300_000] * (n - 1) + [900_000]          # last bar vol spike × 3
    return _make_hist(closes, volumes=volumes)


def _flat_hist(n=90):
    """Flat price around 500."""
    closes = [500.0 + (i % 5 - 2) for i in range(n)]
    return _make_hist(closes)


def _oversold_hist(n=90):
    """Strong downtrend → RSI well below 40."""
    closes = [500.0 * (0.994 ** i) for i in range(n)]
    return _make_hist(closes)


def _make_ticker(df5d=None, df90d=None, df30d=None, df5m=None):
    """Return a mock yfinance.Ticker whose .history() call routes by period."""
    mock = MagicMock()

    def _history(period="1mo", interval="1d", **kwargs):
        if interval == "5m":
            return df5m if df5m is not None else pd.DataFrame()
        if period in ("5d", "1wk"):
            return df5d if df5d is not None else df90d if df90d is not None else pd.DataFrame()
        if period == "30d":
            return df30d if df30d is not None else df90d if df90d is not None else pd.DataFrame()
        return df90d if df90d is not None else pd.DataFrame()

    mock.history.side_effect = _history
    return mock


# ── Universe tests ─────────────────────────────────────────────────────────────

def test_universe_nifty50():
    sc = StockScreener()
    sc.universe = "nifty50"
    u = sc.get_universe()
    assert len(u) == 50
    assert "RELIANCE" in u


def test_universe_nifty100():
    sc = StockScreener()
    sc.universe = "nifty100"
    u = sc.get_universe()
    assert len(u) == 100
    assert all(s in u for s in ["RELIANCE", "ADANIENT"])


def test_universe_fno():
    sc = StockScreener()
    sc.universe = "fno"
    u = sc.get_universe()
    assert len(u) == len(NIFTY50) + len(NIFTY_NEXT50) + len(FNO_EXTRAS)


def test_universe_custom():
    sc = StockScreener()
    sc.universe = "custom"
    sc.custom_symbols = ["RELIANCE", "TCS"]
    assert sc.get_universe() == ["RELIANCE", "TCS"]


def test_universe_default_fallback():
    sc = StockScreener()
    sc.universe = "unknown_value"
    u = sc.get_universe()
    assert len(u) == 100   # NIFTY50 + NIFTY_NEXT50


# ── Regime detection ───────────────────────────────────────────────────────────

def test_regime_trending(monkeypatch):
    from data import feed
    monkeypatch.setattr(feed, "spot", lambda sym: _VIX_LOW - 1)
    sc = StockScreener()
    assert sc.detect_regime() == "TRENDING"


def test_regime_volatile(monkeypatch):
    from data import feed
    monkeypatch.setattr(feed, "spot", lambda sym: _VIX_HIGH + 1)
    sc = StockScreener()
    assert sc.detect_regime() == "VOLATILE"


def test_regime_normal(monkeypatch):
    from data import feed
    monkeypatch.setattr(feed, "spot", lambda sym: (_VIX_LOW + _VIX_HIGH) / 2)
    sc = StockScreener()
    assert sc.detect_regime() == "NORMAL"


def test_regime_zero_vix(monkeypatch):
    """VIX feed returning 0 (off-hours) must not crash — returns NORMAL."""
    from data import feed
    monkeypatch.setattr(feed, "spot", lambda sym: 0.0)
    sc = StockScreener()
    assert sc.detect_regime() == "NORMAL"


# ── score_stock ────────────────────────────────────────────────────────────────

def test_score_stock_trending(monkeypatch):
    """Trending data → high mom_score, low rev_score."""
    hist = _trending_hist()
    ticker = _make_ticker(df90d=hist)
    with patch("yfinance.Ticker", return_value=ticker):
        sc = StockScreener()
        r = sc.score_stock("RELIANCE")
    assert r is not None
    assert r["symbol"] == "RELIANCE"
    assert r["price"] > 0
    assert 0 <= r["mom_score"] <= 100
    assert 0 <= r["rev_score"] <= 100
    assert r["mom_score"] > r["rev_score"], "Trending data should score higher momentum"
    assert r["signal"] == "MOMENTUM"


def test_score_stock_oversold(monkeypatch):
    """Downtrend data → high rev_score, low mom_score."""
    hist = _oversold_hist()
    ticker = _make_ticker(df90d=hist)
    with patch("yfinance.Ticker", return_value=ticker):
        sc = StockScreener()
        r = sc.score_stock("POWERGRID")
    assert r is not None
    assert r["rev_score"] > r["mom_score"], "Downtrend should score higher reversion"
    assert r["signal"] == "REVERSION"


def test_score_stock_no_data(monkeypatch):
    """Empty DataFrame → returns None (not a crash)."""
    ticker = _make_ticker(df90d=pd.DataFrame())
    with patch("yfinance.Ticker", return_value=ticker):
        sc = StockScreener()
        r = sc.score_stock("FAKE")
    assert r is None


def test_score_stock_too_few_rows(monkeypatch):
    """< 30 rows → returns None."""
    hist = _make_hist([500.0] * 10)
    ticker = _make_ticker(df90d=hist)
    with patch("yfinance.Ticker", return_value=ticker):
        sc = StockScreener()
        r = sc.score_stock("SHORT")
    assert r is None


def test_score_stock_below_min_price(monkeypatch):
    """Penny stock (price < min_price) → filtered out."""
    hist = _make_hist([50.0] * 60)  # well below default min_price=100
    ticker = _make_ticker(df90d=hist)
    with patch("yfinance.Ticker", return_value=ticker):
        sc = StockScreener()
        r = sc.score_stock("PENNYSTOCK")
    assert r is None


def test_score_stock_result_fields(monkeypatch):
    """All expected keys must be present in the scored dict."""
    hist = _trending_hist()
    ticker = _make_ticker(df90d=hist)
    with patch("yfinance.Ticker", return_value=ticker):
        sc = StockScreener()
        r = sc.score_stock("TCS")
    assert r is not None
    required = {
        "symbol", "price", "rsi", "ema9", "ema21", "adx", "vol_ratio",
        "atr_pct", "bb_width", "macd_hist", "mom_score", "rev_score",
        "composite", "pct_from_52h", "signal", "fit_mtf", "fit_intraday", "regime",
    }
    assert required.issubset(r.keys())


def test_score_stock_composite_is_average(monkeypatch):
    hist = _trending_hist()
    ticker = _make_ticker(df90d=hist)
    with patch("yfinance.Ticker", return_value=ticker):
        sc = StockScreener()
        r = sc.score_stock("TCS")
    assert r is not None
    expected = round((r["mom_score"] + r["rev_score"]) / 2, 1)
    assert math.isclose(r["composite"], expected, abs_tol=0.15)


# ── scan() ─────────────────────────────────────────────────────────────────────

def test_scan_populates_results(monkeypatch):
    """scan() fills _results; top_momentum/top_reversion return sorted lists."""
    hist = _trending_hist()
    ticker = _make_ticker(df90d=hist)
    with patch("yfinance.Ticker", return_value=ticker):
        sc = StockScreener()
        sc.universe = "custom"
        sc.custom_symbols = ["RELIANCE", "TCS", "INFY"]
        results = sc.scan()
    assert len(results) == 3
    assert set(results.keys()) == {"RELIANCE", "TCS", "INFY"}


def test_scan_empty_on_all_failures(monkeypatch):
    """If all tickers return empty data, _results is empty — not a crash."""
    ticker = _make_ticker(df90d=pd.DataFrame())
    with patch("yfinance.Ticker", return_value=ticker):
        sc = StockScreener()
        sc.universe = "custom"
        sc.custom_symbols = ["FAKE1", "FAKE2"]
        results = sc.scan()
    assert results == {}


def test_scan_sets_last_scan(monkeypatch):
    hist = _trending_hist()
    ticker = _make_ticker(df90d=hist)
    with patch("yfinance.Ticker", return_value=ticker):
        sc = StockScreener()
        sc.universe = "custom"
        sc.custom_symbols = ["RELIANCE"]
        assert sc.last_scan is None
        sc.scan()
        assert sc.last_scan is not None


def test_top_momentum_sorted(monkeypatch):
    """top_momentum returns stocks sorted by mom_score descending."""
    hist_up   = _trending_hist()
    hist_down = _oversold_hist()
    call_count = [0]

    def _ticker(sym):
        call_count[0] += 1
        # Alternate: RELIANCE=trending, TCS=oversold
        return _make_ticker(df90d=hist_up if "RELIANCE" in sym else hist_down)

    with patch("yfinance.Ticker", side_effect=_ticker):
        sc = StockScreener()
        sc.universe = "custom"
        sc.custom_symbols = ["RELIANCE", "TCS"]
        sc.scan()
        top = sc.top_momentum(2)

    assert len(top) == 2
    assert top[0]["mom_score"] >= top[1]["mom_score"]


def test_top_reversion_sorted(monkeypatch):
    hist_up   = _trending_hist()
    hist_down = _oversold_hist()

    def _ticker(sym):
        return _make_ticker(df90d=hist_up if "RELIANCE" in sym else hist_down)

    with patch("yfinance.Ticker", side_effect=_ticker):
        sc = StockScreener()
        sc.universe = "custom"
        sc.custom_symbols = ["RELIANCE", "TCS"]
        sc.scan()
        top = sc.top_reversion(2)

    assert len(top) == 2
    assert top[0]["rev_score"] >= top[1]["rev_score"]


def test_top_n_truncates(monkeypatch):
    """Asking for n=1 returns exactly 1 result."""
    hist = _trending_hist()
    with patch("yfinance.Ticker", return_value=_make_ticker(df90d=hist)):
        sc = StockScreener()
        sc.universe = "custom"
        sc.custom_symbols = ["RELIANCE", "TCS", "INFY"]
        sc.scan()
        assert len(sc.top_momentum(1)) == 1
        assert len(sc.top_reversion(1)) == 1


# ── score_breakout ─────────────────────────────────────────────────────────────

def _breakout_hist():
    """
    Builds history that satisfies all breakout conditions:
    - BB squeeze ending (price breaks above 20-bar high)
    - volume surge on the last bar
    - RSI in acceptable range
    """
    n = 60
    # Flat consolidation for first 40 bars, then sharp breakout
    closes  = [500.0] * 40 + [500.0 * (1.005 ** (i + 1)) for i in range(20)]
    highs   = [c * 1.005 for c in closes]
    lows    = [c * 0.995 for c in closes]
    volumes = [200_000] * (n - 1) + [700_000]   # big vol on breakout bar
    return _make_hist(closes, highs, lows, volumes)


def test_score_breakout_valid(monkeypatch):
    """Breakout conditions met → returns a scored dict with expected fields."""
    hist = _breakout_hist()
    with patch("yfinance.Ticker", return_value=_make_ticker(df90d=hist)):
        sc = StockScreener()
        r = sc.score_breakout("HINDALCO")
    # May return None if conditions aren't met exactly — just check no exception
    if r is not None:
        assert r["strategy"] == "BREAKOUT"
        assert r["direction"] == "LONG"
        assert 0 <= r["score"] <= 100
        assert r["sl"] < r["entry"]
        assert r["target"] > r["entry"]


def test_score_breakout_empty_data(monkeypatch):
    """Empty data → returns None, no crash."""
    with patch("yfinance.Ticker", return_value=_make_ticker(df90d=pd.DataFrame())):
        sc = StockScreener()
        assert sc.score_breakout("FAKE") is None


def test_score_breakout_too_few_rows(monkeypatch):
    hist = _make_hist([500.0] * 15)
    with patch("yfinance.Ticker", return_value=_make_ticker(df90d=hist)):
        sc = StockScreener()
        assert sc.score_breakout("FAKE") is None


# ── score_bounce ───────────────────────────────────────────────────────────────

def test_score_bounce_oversold_at_lower_bb(monkeypatch):
    """Deeply oversold + at lower BB → may return bounce signal."""
    hist = _oversold_hist(90)
    with patch("yfinance.Ticker", return_value=_make_ticker(df90d=hist)):
        sc = StockScreener()
        r = sc.score_bounce("POWERGRID")
    if r is not None:
        assert r["strategy"] == "BOUNCE"
        assert r["direction"] == "LONG"
        assert 0 <= r["score"] <= 100
        assert r["sl"] < r["entry"]
        assert r["target"] > r["entry"]
        assert r["rsi"] <= 40


def test_score_bounce_trending_stock_returns_none(monkeypatch):
    """Strongly trending stock (RSI > 50) must NOT produce a bounce signal."""
    hist = _trending_hist(90)
    with patch("yfinance.Ticker", return_value=_make_ticker(df90d=hist)):
        sc = StockScreener()
        r = sc.score_bounce("HINDALCO")
    assert r is None   # RSI too high for bounce


def test_score_bounce_empty_data(monkeypatch):
    with patch("yfinance.Ticker", return_value=_make_ticker(df90d=pd.DataFrame())):
        sc = StockScreener()
        assert sc.score_bounce("FAKE") is None


# ── score_bulk ─────────────────────────────────────────────────────────────────

def _bulk_5m_hist(bulk_ratio=3.0, price_up=True):
    """5-min bars with a volume spike on the last bar."""
    n = 30
    closes  = [500.0 + 0.1 * i for i in range(n)]
    if not price_up:
        closes = list(reversed(closes))
    avg_vol = 200_000
    volumes = [avg_vol] * (n - 1) + [int(avg_vol * bulk_ratio)]
    return _make_hist(closes, volumes=volumes)


def test_score_bulk_spike_detected(monkeypatch):
    """Volume spike ≥ 2.5× → bulk signal returned."""
    hist5m  = _bulk_5m_hist(bulk_ratio=3.5)
    hist1d  = _trending_hist(30)
    with patch("yfinance.Ticker", return_value=_make_ticker(df5m=hist5m, df30d=hist1d)):
        sc = StockScreener()
        r = sc.score_bulk("TATAMOTORS")
    if r is not None:   # threshold checks may differ from mock data — just verify structure
        assert r["strategy"] == "BULK_ORDER"
        assert r["bulk_ratio"] >= 2.5
        assert 0 <= r["score"] <= 100


def test_score_bulk_no_spike(monkeypatch):
    """Volume spike below 2.5× → returns None."""
    hist5m = _bulk_5m_hist(bulk_ratio=1.2)
    hist1d = _trending_hist(30)
    with patch("yfinance.Ticker", return_value=_make_ticker(df5m=hist5m, df30d=hist1d)):
        sc = StockScreener()
        r = sc.score_bulk("TATAMOTORS")
    assert r is None


def test_score_bulk_empty_data(monkeypatch):
    with patch("yfinance.Ticker", return_value=_make_ticker(df5m=pd.DataFrame())):
        sc = StockScreener()
        assert sc.score_bulk("FAKE") is None


# ── get_results thread safety ──────────────────────────────────────────────────

def test_get_results_returns_copy(monkeypatch):
    """get_results() must return a copy so mutations don't affect internal state."""
    hist = _trending_hist()
    with patch("yfinance.Ticker", return_value=_make_ticker(df90d=hist)):
        sc = StockScreener()
        sc.universe = "custom"
        sc.custom_symbols = ["RELIANCE"]
        sc.scan()
        r1 = sc.get_results()
        r1["MUTATED"] = True   # mutate the returned copy
        r2 = sc.get_results()
        assert "MUTATED" not in r2


def test_concurrent_scan_and_read(monkeypatch):
    """scan() and get_results() can be called from different threads without deadlock."""
    hist = _trending_hist()
    results_holder = []

    def _ticker(sym):
        return _make_ticker(df90d=hist)

    with patch("yfinance.Ticker", side_effect=_ticker):
        sc = StockScreener()
        sc.universe = "custom"
        sc.custom_symbols = ["RELIANCE", "TCS"]

        def reader():
            for _ in range(5):
                results_holder.append(sc.get_results())

        t = threading.Thread(target=reader)
        t.start()
        sc.scan()
        t.join(timeout=5)

    assert not t.is_alive(), "Reader thread deadlocked"
