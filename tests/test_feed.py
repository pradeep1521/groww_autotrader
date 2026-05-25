"""
Tests for data/feed.py

yfinance calls are fully mocked so tests run offline.
"""
import pytest
import sys, os
import threading
import pandas as pd

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))


class _FakeHistory:
    """Pretend DataFrame returned by yf.Ticker().history()."""
    def __init__(self, rows):
        import pandas as pd
        self._df = pd.DataFrame(
            rows, columns=["Open", "High", "Low", "Close", "Volume"]
        )

    @property
    def empty(self):
        return self._df.empty

    def __len__(self):
        return len(self._df)

    # Delegate item access for Close/Volume columns
    def __getitem__(self, key):
        return self._df[key]


class _FakeTicker:
    def __init__(self, rows):
        self._rows = rows

    def history(self, **kwargs):
        return _FakeHistory(self._rows)


def _patch_yf(monkeypatch, rows):
    """Patch yfinance.Ticker inside the data.feed module."""
    import data.feed as feed_module
    import yfinance as yf_real

    class _MockYF:
        @staticmethod
        def Ticker(sym):
            return _FakeTicker(rows)

    monkeypatch.setattr("yfinance.Ticker", lambda sym: _FakeTicker(rows))


# ── _yf_fetch ──────────────────────────────────────────────────────────────────

class TestYfFetch:
    def test_returns_dict_with_price(self, monkeypatch):
        import data.feed as feed
        rows = [
            [100.0, 105.0, 98.0, 102.0, 1_000_000],
            [102.0, 107.0, 100.0, 104.0, 1_200_000],
        ]
        _patch_yf(monkeypatch, rows)
        result = feed._yf_fetch("NIFTY")
        assert result is not None
        assert abs(result["price"] - 104.0) < 0.01

    def test_returns_none_on_empty_history(self, monkeypatch):
        import data.feed as feed
        _patch_yf(monkeypatch, [])
        result = feed._yf_fetch("NIFTY")
        assert result is None

    def test_result_has_required_keys(self, monkeypatch):
        import data.feed as feed
        rows = [
            [100.0, 105.0, 98.0, 102.0, 1_000_000],
            [102.0, 107.0, 100.0, 104.0, 1_200_000],
        ]
        _patch_yf(monkeypatch, rows)
        result = feed._yf_fetch("BANKNIFTY")
        assert result is not None
        for k in ("symbol", "price", "prev_close", "change", "change_pct", "ts"):
            assert k in result

    def test_change_calculated_correctly(self, monkeypatch):
        import data.feed as feed
        rows = [
            [100.0, 105.0, 98.0, 100.0, 1_000_000],   # prev close = 100
            [100.0, 107.0, 100.0, 110.0, 1_200_000],   # close = 110
        ]
        _patch_yf(monkeypatch, rows)
        result = feed._yf_fetch("NIFTY")
        assert result is not None
        assert abs(result["change"] - 10.0) < 0.01
        assert abs(result["change_pct"] - 10.0) < 0.1


# ── get_price / spot ───────────────────────────────────────────────────────────

class TestGetPrice:
    def test_get_price_returns_dict(self, monkeypatch):
        import data.feed as feed
        feed._cache.clear()
        rows = [
            [100.0, 105.0, 98.0, 200.0, 1_000_000],
            [200.0, 210.0, 198.0, 205.0, 1_200_000],
        ]
        _patch_yf(monkeypatch, rows)
        result = feed.get_price("NIFTY_GP")
        assert result is not None
        assert result["price"] == 205.0

    def test_spot_returns_float(self, monkeypatch):
        import data.feed as feed
        feed._cache.clear()
        rows = [
            [100.0, 105.0, 98.0, 300.0, 1_000_000],
            [300.0, 310.0, 298.0, 310.0, 1_200_000],
        ]
        _patch_yf(monkeypatch, rows)
        price = feed.spot("NIFTY_SP")
        assert isinstance(price, float)
        assert price == 310.0

    def test_spot_returns_0_on_failure(self, monkeypatch):
        import data.feed as feed
        feed._cache.clear()
        _patch_yf(monkeypatch, [])
        assert feed.spot("UNKNOWN_SYM") == 0.0


# ── refresh ────────────────────────────────────────────────────────────────────

class TestRefresh:
    def test_refresh_updates_cache(self, monkeypatch):
        import data.feed as feed
        feed._cache.clear()
        rows = [
            [100.0, 105.0, 98.0, 22000.0, 1_000_000],
            [22000.0, 22100.0, 21900.0, 22050.0, 2_000_000],
        ]
        _patch_yf(monkeypatch, rows)
        result = feed.refresh("NIFTY_R")
        assert result is not None
        cached = feed.get_price("NIFTY_R")
        assert cached is not None
        assert cached["price"] == result["price"]


# ── Symbol mapping ─────────────────────────────────────────────────────────────

class TestSymbolMapping:
    def test_nifty_maps_to_nsei(self):
        import data.feed as feed
        assert feed._YF_SYMBOLS["NIFTY"] == "^NSEI"

    def test_banknifty_maps_to_nsebank(self):
        import data.feed as feed
        assert feed._YF_SYMBOLS["BANKNIFTY"] == "^NSEBANK"

    def test_vix_maps_to_indiavix(self):
        import data.feed as feed
        assert feed._YF_SYMBOLS["VIX"] == "^INDIAVIX"

    def test_unknown_symbol_gets_ns_suffix(self, monkeypatch):
        """Symbols not in the map should get .NS appended."""
        import data.feed as feed
        captured = []

        def fake_ticker(sym):
            captured.append(sym)
            return _FakeTicker([])

        monkeypatch.setattr("yfinance.Ticker", fake_ticker)
        feed._yf_fetch("RELIANCE")
        assert captured and captured[0] == "RELIANCE.NS"


# ── batch_refresh ──────────────────────────────────────────────────────────────

class TestBatchRefresh:
    def test_returns_dict_of_results(self, monkeypatch):
        import data.feed as feed
        rows = [
            [100.0, 105.0, 98.0, 500.0, 1_000_000],
            [500.0, 510.0, 498.0, 505.0, 1_200_000],
        ]
        _patch_yf(monkeypatch, rows)
        result = feed.batch_refresh(["RELIANCE", "TCS"])
        assert isinstance(result, dict)
        # Each symbol that fetches data appears in result
        for sym in result:
            assert "price" in result[sym]

    def test_empty_list_returns_empty_dict(self, monkeypatch):
        import data.feed as feed
        result = feed.batch_refresh([])
        assert result == {}
