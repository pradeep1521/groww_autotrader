"""
Risk Guard — Portfolio-Level Risk Management
============================================
Provides:
  • ATR-based SL and target prices (adapts to stock's own volatility)
  • VIX-adaptive multipliers (widens SL in high-vol regimes)
  • Kelly-style position sizing (risk a fixed % of capital per trade)
  • Portfolio exposure cap (max % of capital deployed at once)
  • Max concurrent open trades guard

Usage
-----
    from engine.risk_guard import risk_guard

    # ATR-based SL and target
    sl  = risk_guard.sl_price(entry=2800, atr=35.0, side="BUY")   # 2730
    tgt = risk_guard.target_price(entry=2800, atr=35.0, side="BUY") # 2905

    # VIX-adaptive sizing
    qty = risk_guard.position_size(entry=2800, atr=35.0, vix=18)   # 28 shares
"""

from typing import Optional


class RiskGuard:
    """
    Stateless (all methods are pure) risk calculator.
    Adjust class attributes to tune risk appetite.
    """

    # ── Configuration ──────────────────────────────────────────────────────────

    capital:             float = 500_000.0   # Total trading capital (₹)
    risk_per_trade_pct:  float = 1.0         # Max loss per trade = 1% of capital
    atr_sl_mult:         float = 2.0         # SL = entry ± 2× ATR
    atr_tgt_mult:        float = 3.0         # Target = entry ± 3× ATR (1.5:1 R:R)
    max_open_trades:     int   = 6           # Max simultaneous positions
    max_portfolio_pct:   float = 15.0        # Max % of capital in any single stock
    max_total_exposure:  float = 80.0        # Max % of capital deployed at once

    # ── ATR-based SL / Target ─────────────────────────────────────────────────

    def sl_price(self, entry: float, atr: float,
                 side: str = "BUY", vix: float = 0.0) -> float:
        """Stop-loss price scaled by ATR (and VIX if provided)."""
        mult = self.vix_adjusted_sl_mult(vix) if vix > 0 else self.atr_sl_mult
        dist = round(mult * atr, 2)
        return round(entry - dist if side == "BUY" else entry + dist, 2)

    def target_price(self, entry: float, atr: float,
                     side: str = "BUY", vix: float = 0.0) -> float:
        """Target price that maintains ≥1.5:1 R:R versus the ATR-based SL."""
        sl_mult  = self.vix_adjusted_sl_mult(vix) if vix > 0 else self.atr_sl_mult
        tgt_mult = round(sl_mult * 1.5, 2)          # always 1.5:1 R:R
        dist     = round(tgt_mult * atr, 2)
        return round(entry + dist if side == "BUY" else entry - dist, 2)

    def sl_pct(self, entry: float, atr: float, vix: float = 0.0) -> float:
        """SL distance as % of entry price."""
        if entry <= 0:
            return 1.0
        mult = self.vix_adjusted_sl_mult(vix) if vix > 0 else self.atr_sl_mult
        return round(mult * atr / entry * 100, 2)

    def target_pct(self, entry: float, atr: float, vix: float = 0.0) -> float:
        """Target distance as % of entry price."""
        if entry <= 0:
            return 1.5
        sl_mult  = self.vix_adjusted_sl_mult(vix) if vix > 0 else self.atr_sl_mult
        tgt_mult = sl_mult * 1.5
        return round(tgt_mult * atr / entry * 100, 2)

    # ── VIX-adaptive multipliers ───────────────────────────────────────────────

    def vix_adjusted_sl_mult(self, vix: float) -> float:
        """
        Widen SL in high-volatility environments to avoid noise stop-outs.
          VIX ≤ 12 → 1.5×    (very calm — tight stops fine)
          VIX 15   → 2.0×
          VIX 20   → 2.5×
          VIX 30+  → 3.5×    (very volatile — need wider stops)
        """
        if vix <= 0:
            return self.atr_sl_mult
        val = 1.0 + (vix / 10.0)           # 1.2 at VIX=2, 4.0 at VIX=30
        return round(min(max(val, 1.5), 4.0), 2)

    # ── Position sizing ────────────────────────────────────────────────────────

    def position_size(self, entry: float, atr: float,
                      vix: float = 0.0,
                      open_trades: int = 0) -> int:
        """
        Calculate qty so that hitting SL loses at most risk_per_trade_pct% of capital.

        Formula:
            risk_amount  = capital × risk_per_trade_pct / 100
            sl_distance  = vix_adjusted_sl_mult × ATR
            qty          = risk_amount / sl_distance

        Also capped by:
          • max_portfolio_pct  — no single position > N% of capital
          • max_total_exposure — if many trades open, scale down
        """
        if entry <= 0 or atr <= 0:
            return 1
        sl_mult      = self.vix_adjusted_sl_mult(vix) if vix > 0 else self.atr_sl_mult
        risk_amount  = self.capital * self.risk_per_trade_pct / 100
        sl_dist      = sl_mult * atr
        qty_by_risk  = max(1, int(risk_amount / sl_dist))

        # Single stock exposure cap
        qty_by_exp   = max(1, int(self.capital * self.max_portfolio_pct / 100 / entry))

        # Scale down if many trades are already open (preserve portfolio exposure limit)
        if open_trades > 0:
            total_budget = self.capital * self.max_total_exposure / 100
            per_trade    = total_budget / (open_trades + 1)
            qty_budget   = max(1, int(per_trade / entry))
        else:
            qty_budget   = qty_by_exp

        return min(qty_by_risk, qty_by_exp, qty_budget)

    # ── Portfolio guards ───────────────────────────────────────────────────────

    def can_add_trade(self, open_trades: int,
                      new_value: float = 0.0,
                      deployed: float = 0.0) -> tuple[bool, str]:
        """
        Returns (allowed, reason).
          open_trades — current number of open positions
          new_value   — cost of the new trade (entry × qty)
          deployed    — total capital currently deployed
        """
        if open_trades >= self.max_open_trades:
            return False, f"Max {self.max_open_trades} concurrent trades reached"
        if new_value > 0 and deployed + new_value > self.capital * self.max_total_exposure / 100:
            return False, (
                f"Adding this trade would exceed max portfolio exposure "
                f"({self.max_total_exposure:.0f}% of ₹{self.capital:,.0f})"
            )
        return True, "OK"

    # ── Options-specific sizing ────────────────────────────────────────────────

    def options_qty(self, lot_size: int, lots: int,
                    premium: float, vix: float = 0.0) -> dict:
        """
        For options trades: check if the total premium cost fits within
        risk_per_trade_pct of capital. Auto-suggest max safe lots.
        premium = single lot cost (premium × lot_size).
        """
        risk_budget   = self.capital * self.risk_per_trade_pct / 100
        vix_scale     = 1.0 + max(0, (vix - 15) / 15) if vix > 0 else 1.0  # widen budget in high VIX
        adj_budget    = risk_budget * vix_scale
        max_lots      = max(1, int(adj_budget / (premium * lot_size))) if premium > 0 else lots
        actual_lots   = min(lots, max_lots)
        return {
            "lots":        actual_lots,
            "qty":         actual_lots * lot_size,
            "cost":        round(premium * actual_lots * lot_size, 2),
            "risk_budget": round(adj_budget, 2),
            "max_lots":    max_lots,
        }

    # ── Breakeven & R:R ───────────────────────────────────────────────────────

    def rr_ratio(self, entry: float, sl: float, target: float,
                 side: str = "BUY") -> float:
        """Return reward-to-risk ratio (e.g. 2.3 means R:R = 2.3:1)."""
        risk   = abs(entry - sl)
        reward = abs(target - entry)
        return round(reward / risk, 2) if risk > 0 else 0.0

    def summary(self, symbol: str, entry: float, atr: float,
                 side: str = "BUY", vix: float = 0.0,
                 open_trades: int = 0) -> dict:
        """Convenience: return all key risk metrics in one call."""
        qty = self.position_size(entry, atr, vix, open_trades)
        sl  = self.sl_price(entry, atr, side, vix)
        tgt = self.target_price(entry, atr, side, vix)
        return {
            "symbol":    symbol,
            "entry":     entry,
            "sl":        sl,
            "target":    tgt,
            "qty":       qty,
            "sl_pct":    self.sl_pct(entry, atr, vix),
            "tgt_pct":   self.target_pct(entry, atr, vix),
            "rr":        self.rr_ratio(entry, sl, tgt, side),
            "max_loss":  round(abs(entry - sl) * qty, 2),
            "max_gain":  round(abs(tgt - entry) * qty, 2),
            "atr":       atr,
            "vix":       vix,
        }


risk_guard = RiskGuard()
