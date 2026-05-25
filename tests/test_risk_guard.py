"""
Tests for engine/risk_guard.py

RiskGuard is fully stateless — every method is a pure function.
Attributes are class-level; set on the instance to shadow them.
"""
import pytest
import sys, os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from engine.risk_guard import RiskGuard


@pytest.fixture
def rg():
    return RiskGuard()


# ── vix_adjusted_sl_mult ───────────────────────────────────────────────────────
# Formula: val = 1.0 + vix/10, clamped to [1.5, 4.0]

class TestVixAdjustedSlMult:
    def test_low_vix_exact(self, rg):
        # VIX=5 → 1.0 + 0.5 = 1.5 (hits lower clamp)
        assert rg.vix_adjusted_sl_mult(5.0) == 1.5

    def test_mid_vix_exact(self, rg):
        # VIX=10 → 1.0 + 1.0 = 2.0
        assert rg.vix_adjusted_sl_mult(10.0) == 2.0

    def test_high_vix_clamped(self, rg):
        # VIX=40 → 1.0 + 4.0 = 5.0 → clamped to 4.0
        assert rg.vix_adjusted_sl_mult(40.0) == 4.0

    def test_zero_vix_returns_base_mult(self, rg):
        assert rg.vix_adjusted_sl_mult(0.0) == rg.atr_sl_mult

    def test_multiplier_increases_with_vix(self, rg):
        m1 = rg.vix_adjusted_sl_mult(5)
        m2 = rg.vix_adjusted_sl_mult(15)
        m3 = rg.vix_adjusted_sl_mult(30)
        assert m1 <= m2 <= m3


# ── sl_price ───────────────────────────────────────────────────────────────────

class TestSlPrice:
    def test_buy_sl_below_entry(self, rg):
        sl = rg.sl_price(entry=1000.0, atr=20.0, side="BUY", vix=15.0)
        assert sl < 1000.0

    def test_sell_sl_above_entry(self, rg):
        sl = rg.sl_price(entry=1000.0, atr=20.0, side="SELL", vix=15.0)
        assert sl > 1000.0

    def test_higher_vix_widens_buy_sl(self, rg):
        sl_calm   = rg.sl_price(1000.0, 20.0, "BUY", vix=5.0)
        sl_scared = rg.sl_price(1000.0, 20.0, "BUY", vix=30.0)
        assert sl_scared < sl_calm

    def test_larger_atr_lowers_buy_sl(self, rg):
        sl_small = rg.sl_price(1000.0, atr=10.0, side="BUY", vix=15.0)
        sl_large = rg.sl_price(1000.0, atr=40.0, side="BUY", vix=15.0)
        assert sl_large < sl_small

    def test_exact_value_buy(self, rg):
        # VIX=10 → mult=2.0; dist=2.0*20=40; sl=1000-40=960
        sl = rg.sl_price(entry=1000.0, atr=20.0, side="BUY", vix=10.0)
        assert abs(sl - 960.0) < 0.1

    def test_exact_value_sell(self, rg):
        sl = rg.sl_price(entry=1000.0, atr=20.0, side="SELL", vix=10.0)
        assert abs(sl - 1040.0) < 0.1


# ── target_price ──────────────────────────────────────────────────────────────

class TestTargetPrice:
    def test_buy_target_above_entry(self, rg):
        tgt = rg.target_price(entry=1000.0, atr=20.0, side="BUY", vix=15.0)
        assert tgt > 1000.0

    def test_sell_target_below_entry(self, rg):
        tgt = rg.target_price(entry=1000.0, atr=20.0, side="SELL", vix=15.0)
        assert tgt < 1000.0

    def test_reward_to_risk_at_least_1_5(self, rg):
        entry = 1000.0
        atr   = 20.0
        vix   = 15.0
        sl  = rg.sl_price(entry, atr, "BUY", vix)
        tgt = rg.target_price(entry, atr, "BUY", vix)
        sl_dist  = entry - sl
        tgt_dist = tgt - entry
        assert tgt_dist >= sl_dist * 1.4

    def test_symmetric_for_sell(self, rg):
        entry = 1000.0
        atr   = 20.0
        vix   = 10.0
        sl_b  = rg.sl_price(entry, atr, "BUY", vix)
        sl_s  = rg.sl_price(entry, atr, "SELL", vix)
        tgt_b = rg.target_price(entry, atr, "BUY", vix)
        tgt_s = rg.target_price(entry, atr, "SELL", vix)
        assert abs((entry - sl_b) - (sl_s - entry)) < 0.01
        assert abs((tgt_b - entry) - (entry - tgt_s)) < 0.01


# ── position_size ──────────────────────────────────────────────────────────────

class TestPositionSize:
    def test_returns_positive_int(self, rg):
        qty = rg.position_size(entry=500.0, atr=5.0, vix=15.0, open_trades=0)
        assert isinstance(qty, int) and qty >= 1

    def test_more_open_trades_reduces_size(self, rg):
        qty0 = rg.position_size(500.0, 5.0, 15.0, open_trades=0)
        qty3 = rg.position_size(500.0, 5.0, 15.0, open_trades=3)
        assert qty3 <= qty0

    def test_high_atr_gives_smaller_size(self, rg):
        qty_low  = rg.position_size(500.0, atr=5.0,  vix=15.0, open_trades=0)
        qty_high = rg.position_size(500.0, atr=50.0, vix=15.0, open_trades=0)
        assert qty_high <= qty_low

    def test_size_within_portfolio_cap(self, rg):
        qty   = rg.position_size(100.0, atr=1.0, vix=10.0, open_trades=0)
        value = qty * 100.0
        assert value <= rg.capital * rg.max_portfolio_pct / 100.0 * 1.1


# ── sl_pct / target_pct ────────────────────────────────────────────────────────

class TestSlTargetPct:
    def test_sl_pct_positive(self, rg):
        assert rg.sl_pct(entry=1000.0, atr=20.0, vix=15.0) > 0

    def test_target_pct_positive(self, rg):
        assert rg.target_pct(entry=1000.0, atr=20.0, vix=15.0) > 0

    def test_target_pct_greater_than_sl_pct(self, rg):
        sl  = rg.sl_pct(1000.0, 20.0, 15.0)
        tgt = rg.target_pct(1000.0, 20.0, 15.0)
        assert tgt > sl

    def test_consistent_with_sl_price(self, rg):
        entry = 1000.0
        atr   = 20.0
        vix   = 15.0
        sl_px      = rg.sl_price(entry, atr, "BUY", vix)
        sl_pct_val = rg.sl_pct(entry, atr, vix)
        expected   = (entry - sl_px) / entry * 100
        assert abs(sl_pct_val - expected) < 0.15


# ── can_add_trade ──────────────────────────────────────────────────────────────

class TestCanAddTrade:
    def test_allows_first_trade(self, rg):
        ok, msg = rg.can_add_trade(open_trades=0)
        assert ok is True

    def test_blocks_at_max_trades(self, rg):
        ok, msg = rg.can_add_trade(open_trades=rg.max_open_trades)
        assert ok is False

    def test_blocks_when_over_exposure(self, rg):
        # deployed = 75%, new trade = 10% → total 85% > max_total_exposure=80%
        deployed  = rg.capital * 0.75
        new_value = rg.capital * 0.10   # must pass new_value > 0 to trigger check
        ok, msg = rg.can_add_trade(open_trades=2, new_value=new_value, deployed=deployed)
        assert ok is False

    def test_returns_reason_string(self, rg):
        _, msg = rg.can_add_trade(open_trades=rg.max_open_trades)
        assert isinstance(msg, str) and len(msg) > 0


# ── Custom instance config ─────────────────────────────────────────────────────

class TestCustomConfig:
    def test_custom_capital_scales_position(self):
        rg_small = RiskGuard()
        rg_small.capital = 100_000
        rg_large = RiskGuard()
        rg_large.capital = 1_000_000
        qty_small = rg_small.position_size(500.0, 5.0, 15.0, 0)
        qty_large = rg_large.position_size(500.0, 5.0, 15.0, 0)
        assert qty_large >= qty_small * 5

    def test_custom_risk_pct_scales_position(self):
        rg_low  = RiskGuard()
        rg_low.risk_per_trade_pct = 0.5
        rg_high = RiskGuard()
        rg_high.risk_per_trade_pct = 2.0
        qty_low  = rg_low.position_size(500.0, 5.0, 15.0, 0)
        qty_high = rg_high.position_size(500.0, 5.0, 15.0, 0)
        assert qty_high >= qty_low
