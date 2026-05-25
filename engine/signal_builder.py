"""
Signal Builder
==============
Converts raw screener scores into actionable, plain-English trade signals.

Each stock signal has:
    symbol, direction (BUY/SELL), price, stop_loss, target,
    risk_reward, reason (plain English), rsi, score

Each options signal has:
    symbol, direction (BUY CALL / BUY PUT), strike, opt_type,
    spot, pcr, max_pain, reason
"""

from __future__ import annotations

from engine.risk_guard import risk_guard
from engine import options_chain as oc
from data import feed


# ─────────────────────────────────────────────────────────────────────────────
# Stock signals
# ─────────────────────────────────────────────────────────────────────────────

def stock_signals(n: int = 8) -> list[dict]:
    """
    Return up to *n* stock BUY signals from the screener cache.

    Prioritises momentum setups first, then oversold-bounce setups.
    Returns an empty list if no scan has been run yet.
    """
    # Lazy import avoids circular imports and speeds app startup
    from engine.screener import screener

    results = screener.get_results()
    if not results:
        return []

    vix = feed.spot("VIX") or 16.0
    signals: list[dict] = []

    # Rank by composite score (higher = stronger setup)
    ranked = sorted(results.values(), key=lambda x: x["composite"], reverse=True)

    for r in ranked:
        sig = _build_stock_signal(r, vix)
        if sig:
            signals.append(sig)
        if len(signals) >= n:
            break

    return signals


def _build_stock_signal(r: dict, vix: float) -> dict | None:
    """Build one signal dict from a screener result row, or return None."""
    sym   = r["symbol"]
    price = r["price"]
    if price <= 0:
        return None

    atr_val = price * r.get("atr_pct", 1.5) / 100

    mom = r["mom_score"]
    rev = r["rev_score"]

    if r["signal"] == "MOMENTUM" and mom >= 50:
        direction = "BUY"
        reason    = _momentum_reason(r)
    elif r["signal"] == "REVERSION" and rev >= 45:
        direction = "BUY"
        reason    = _reversion_reason(r)
    else:
        return None

    sl  = risk_guard.sl_price(price, atr_val, "BUY", vix)
    tgt = risk_guard.target_price(price, atr_val, "BUY", vix)

    risk   = abs(price - sl)
    reward = abs(tgt - price)
    rr     = round(reward / risk, 1) if risk > 0 else 0.0

    return {
        "type":        "STOCK",
        "symbol":      sym,
        "direction":   direction,
        "price":       price,
        "sl":          round(sl, 2),
        "target":      round(tgt, 2),
        "risk_reward": rr,
        "reason":      reason,
        "score":       r["composite"],
        "rsi":         round(r.get("rsi", 0), 1),
        "mom_score":   mom,
        "rev_score":   rev,
        "setup":       r["signal"],        # "MOMENTUM" or "REVERSION"
    }


# ─────────────────────────────────────────────────────────────────────────────
# Options signals
# ─────────────────────────────────────────────────────────────────────────────

def options_signals() -> list[dict]:
    """
    Return BUY CALL / BUY PUT signals for NIFTY and BANKNIFTY.

    Returns empty list outside market hours or when NSE API is unavailable.
    """
    signals: list[dict] = []

    for sym in ["NIFTY", "BANKNIFTY"]:
        sig = _build_options_signal(sym)
        if sig:
            signals.append(sig)

    return signals


def _build_options_signal(sym: str) -> dict | None:
    try:
        spot = feed.spot(sym)
        if spot <= 0:
            return None

        df = oc.parse_chain(sym)          # expiry=None → nearest expiry
        if df is None or df.empty:
            return None

        sig = oc.get_signal(df, spot, mode="PCR")
        if not sig:
            return None

        direction = sig.get("direction", "WAIT")
        if direction == "WAIT":
            return None

        pcr_val = sig.get("pcr_val", 1.0)
        mp      = sig.get("max_pain_k", 0)
        atm     = sig.get("atm_strike", int(round(spot, -2)))

        # Strike selection: one step OTM from ATM
        step = 50 if sym == "NIFTY" else 100

        if direction == "BUY_CE":
            strike   = atm + step
            opt_type = "CE"
            label    = "BUY CALL (CE)"
            color    = "BUY"
            reason   = (
                f"PCR {pcr_val:.2f} — put writers dominant, market leaning bullish"
                f" · MaxPain {mp:,}"
            )
        elif direction == "BUY_PE":
            strike   = atm - step
            opt_type = "PE"
            label    = "BUY PUT (PE)"
            color    = "SELL"
            reason   = (
                f"PCR {pcr_val:.2f} — call writers dominant, market leaning bearish"
                f" · MaxPain {mp:,}"
            )
        elif direction == "SELL_STRADDLE":
            strike   = atm
            opt_type = "CE+PE"
            label    = "SELL STRADDLE (range-bound)"
            color    = "NEUTRAL"
            reason   = (
                f"PCR {pcr_val:.2f} — balanced OI, market likely to stay rangebound"
                f" · MaxPain {mp:,}"
            )
        else:
            return None

        return {
            "type":      "OPTIONS",
            "symbol":    sym,
            "direction": label,
            "color":     color,
            "spot":      spot,
            "strike":    strike,
            "opt_type":  opt_type,
            "reason":    reason,
            "pcr":       pcr_val,
            "max_pain":  mp,
            "atm":       atm,
        }
    except Exception:
        return None


# ─────────────────────────────────────────────────────────────────────────────
# Plain-English reason builders
# ─────────────────────────────────────────────────────────────────────────────

def _momentum_reason(r: dict) -> str:
    parts: list[str] = []

    rsi = r.get("rsi", 50)
    if rsi >= 65:
        parts.append(f"RSI {rsi:.0f} — strong uptrend")
    elif rsi >= 55:
        parts.append(f"RSI {rsi:.0f} — building momentum")
    else:
        parts.append(f"RSI {rsi:.0f}")

    vol = r.get("vol_ratio")
    if vol and vol >= 2.0:
        parts.append(f"volume {vol:.1f}× above average — big interest")
    elif vol and vol >= 1.5:
        parts.append(f"volume {vol:.1f}× average")

    adx = r.get("adx")
    if adx and adx >= 30:
        parts.append(f"ADX {adx:.0f} — strong trend")
    elif adx and adx >= 25:
        parts.append(f"ADX {adx:.0f} — trending")

    macd_h = r.get("macd_hist")
    if macd_h and macd_h > 0:
        parts.append("MACD bullish")

    pct = r.get("pct_from_52h", 0)
    if -3 <= pct <= 0:
        parts.append("near 52-week high")

    return " · ".join(parts) if parts else "Momentum setup"


def _reversion_reason(r: dict) -> str:
    parts: list[str] = []

    rsi = r.get("rsi", 50)
    if rsi <= 25:
        parts.append(f"RSI {rsi:.0f} — deeply oversold, bounce likely")
    elif rsi <= 32:
        parts.append(f"RSI {rsi:.0f} — oversold")
    elif rsi <= 40:
        parts.append(f"RSI {rsi:.0f} — pullback opportunity")

    pct = r.get("pct_from_52h", 0)
    if pct <= -20:
        parts.append(f"{abs(pct):.0f}% below 52-week high — deep correction")
    elif pct <= -10:
        parts.append(f"{abs(pct):.0f}% correction from high")

    vol = r.get("vol_ratio")
    if vol and vol <= 0.6:
        parts.append("selling drying up (very low volume)")
    elif vol and vol <= 0.8:
        parts.append("low volume — selling exhausting")

    macd_h = r.get("macd_hist")
    if macd_h and macd_h > 0:
        parts.append("MACD turning up")

    return " · ".join(parts) if parts else "Oversold bounce setup"
