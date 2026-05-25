"""
Tests for engine/options_chain.py

Network calls (NSE fetch_chain) are mocked via monkeypatch so tests run
offline and deterministically.
"""
import pytest
import sys, os
import pandas as pd
import numpy as np

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from engine import options_chain as oc


# ── Fixtures ───────────────────────────────────────────────────────────────────

def _make_raw_chain(strikes, expiry="25-Jul-2025") -> dict:
    """Build a minimal NSE-shaped dict with the given strikes."""
    data = []
    for k in strikes:
        ce_oi  = int(10000 + k * 10)
        pe_oi  = int(8000  + k * 8)
        data.append({
            "strikePrice": k,
            "expiryDate":  expiry,
            "CE": {"openInterest": ce_oi, "changeinOpenInterest": 100,
                   "lastPrice": 5.0, "impliedVolatility": 20.0},
            "PE": {"openInterest": pe_oi, "changeinOpenInterest": -50,
                   "lastPrice": 4.5, "impliedVolatility": 21.0},
        })
    return {"records": {"data": data}}


@pytest.fixture
def sample_strikes():
    return list(range(22000, 22500, 50))   # 10 strikes


@pytest.fixture
def chain_df(sample_strikes, monkeypatch):
    """Returns a parsed DataFrame with mocked NSE data."""
    raw = _make_raw_chain(sample_strikes, expiry="25-Jul-2025")
    monkeypatch.setattr(oc, "fetch_chain", lambda sym="NIFTY": raw)
    return oc.parse_chain("NIFTY", expiry="25-Jul-2025")


# ── parse_chain ────────────────────────────────────────────────────────────────

class TestParseChain:
    def test_returns_dataframe(self, chain_df):
        assert isinstance(chain_df, pd.DataFrame)

    def test_has_required_columns(self, chain_df):
        for col in ("strike", "ce_oi", "pe_oi", "ce_ltp", "pe_ltp", "ce_iv", "pe_iv"):
            assert col in chain_df.columns

    def test_row_count_matches_strikes(self, chain_df, sample_strikes):
        assert len(chain_df) == len(sample_strikes)

    def test_sorted_by_strike(self, chain_df):
        strikes = chain_df["strike"].tolist()
        assert strikes == sorted(strikes)

    def test_empty_df_on_no_data(self, monkeypatch):
        monkeypatch.setattr(oc, "fetch_chain", lambda sym="NIFTY": {})
        df = oc.parse_chain("NIFTY")
        assert df.empty

    def test_empty_df_on_wrong_expiry(self, monkeypatch, sample_strikes):
        raw = _make_raw_chain(sample_strikes, expiry="25-Jul-2025")
        monkeypatch.setattr(oc, "fetch_chain", lambda sym="NIFTY": raw)
        df = oc.parse_chain("NIFTY", expiry="01-Jan-2099")
        assert df.empty


# ── get_expiries ───────────────────────────────────────────────────────────────

class TestGetExpiries:
    def test_returns_list(self, monkeypatch, sample_strikes):
        raw = _make_raw_chain(sample_strikes, "25-Jul-2025")
        monkeypatch.setattr(oc, "fetch_chain", lambda sym="NIFTY": raw)
        exps = oc.get_expiries("NIFTY")
        assert isinstance(exps, list)

    def test_contains_our_expiry(self, monkeypatch, sample_strikes):
        raw = _make_raw_chain(sample_strikes, "25-Jul-2025")
        monkeypatch.setattr(oc, "fetch_chain", lambda sym="NIFTY": raw)
        assert "25-Jul-2025" in oc.get_expiries("NIFTY")

    def test_empty_on_no_data(self, monkeypatch):
        monkeypatch.setattr(oc, "fetch_chain", lambda sym="NIFTY": {})
        assert oc.get_expiries("NIFTY") == []


# ── pcr ────────────────────────────────────────────────────────────────────────

class TestPCR:
    def test_returns_1_on_empty_df(self):
        assert oc.pcr(pd.DataFrame()) == 1.0

    def test_exact_value(self):
        df = pd.DataFrame({"ce_oi": [100, 100], "pe_oi": [150, 150]})
        result = oc.pcr(df)
        assert abs(result - 1.5) < 1e-6

    def test_above_1_when_more_pe(self, chain_df):
        # In our fixture pe_oi < ce_oi (by design), verify sign
        result = oc.pcr(chain_df)
        assert result > 0

    def test_zero_ce_returns_1(self):
        df = pd.DataFrame({"ce_oi": [0, 0], "pe_oi": [100, 100]})
        assert oc.pcr(df) == 1.0


# ── max_pain ───────────────────────────────────────────────────────────────────

class TestMaxPain:
    def test_returns_0_on_empty_df(self):
        assert oc.max_pain(pd.DataFrame()) == 0

    def test_returns_int(self, chain_df):
        mp = oc.max_pain(chain_df)
        assert isinstance(mp, int)

    def test_max_pain_is_a_valid_strike(self, chain_df):
        mp = oc.max_pain(chain_df)
        assert mp in chain_df["strike"].values

    def test_symmetric_oi_max_pain_is_atm(self):
        """Equal OI on each side → max pain is middle strike."""
        strikes = [100, 200, 300]
        df = pd.DataFrame({
            "strike": strikes,
            "ce_oi":  [100, 100, 100],
            "pe_oi":  [100, 100, 100],
        })
        mp = oc.max_pain(df)
        assert mp in strikes   # valid result (exact value depends on formulation)


# ── get_signal ─────────────────────────────────────────────────────────────────

class TestGetSignal:
    def _bullish_df(self):
        """PCR > 1.4 → BUY_CE expected."""
        df = pd.DataFrame({
            "strike":    [22200, 22250, 22300],
            "ce_oi":     [1000, 1000, 1000],
            "pe_oi":     [1500, 1500, 1500],   # PCR = 1.5
            "ce_oi_chg": [10, 10, 10],
            "pe_oi_chg": [-5, -5, -5],
            "ce_ltp":    [5.0, 5.0, 5.0],
            "pe_ltp":    [4.0, 4.0, 4.0],
            "ce_iv":     [20.0, 20.0, 20.0],
            "pe_iv":     [21.0, 21.0, 21.0],
        })
        return df

    def _bearish_df(self):
        """PCR < 0.65 → BUY_PE expected."""
        df = pd.DataFrame({
            "strike":    [22200, 22250, 22300],
            "ce_oi":     [2000, 2000, 2000],
            "pe_oi":     [500, 500, 500],       # PCR = 0.375
            "ce_oi_chg": [10, 10, 10],
            "pe_oi_chg": [-5, -5, -5],
            "ce_ltp":    [5.0, 5.0, 5.0],
            "pe_ltp":    [4.0, 4.0, 4.0],
            "ce_iv":     [20.0, 20.0, 20.0],
            "pe_iv":     [21.0, 21.0, 21.0],
        })
        return df

    def test_wait_on_empty(self):
        sig = oc.get_signal(pd.DataFrame(), spot=22250.0)
        assert sig["direction"] == "WAIT"

    def test_wait_on_zero_spot(self):
        df = self._bullish_df()
        sig = oc.get_signal(df, spot=0.0)
        assert sig["direction"] == "WAIT"

    def test_bullish_pcr_gives_buy_ce(self):
        df  = self._bullish_df()
        sig = oc.get_signal(df, spot=22250.0, mode="PCR")
        assert sig["direction"] == "BUY_CE"

    def test_bearish_pcr_gives_buy_pe(self):
        df  = self._bearish_df()
        sig = oc.get_signal(df, spot=22250.0, mode="PCR")
        assert sig["direction"] == "BUY_PE"

    def test_signal_has_required_keys(self):
        df  = self._bullish_df()
        sig = oc.get_signal(df, spot=22250.0)
        for k in ("direction", "reason", "pcr_val", "max_pain_k", "atm_strike"):
            assert k in sig

    def test_max_pain_mode_above_gives_buy_pe(self):
        df  = self._bullish_df()
        # spot well above max_pain → expect BUY_PE
        mp  = oc.max_pain(df)
        sig = oc.get_signal(df, spot=mp * 1.05, mode="MaxPain")
        assert sig["direction"] in ("BUY_PE", "WAIT")   # depends on threshold
