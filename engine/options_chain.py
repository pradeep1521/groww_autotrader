"""
NSE Options Chain — live data + signal generation.

Data source: NSE India's unofficial public API (same source used by nseindia.com).
Session cookies are refreshed automatically every 5 minutes.
Results are cached for 60 seconds to avoid hammering the API.

Public API
----------
    fetch_chain(symbol)         → raw dict from NSE
    get_expiries(symbol)        → sorted list[str] of expiry date strings
    parse_chain(symbol, expiry) → pd.DataFrame  (strike, ce_*, pe_*)
    pcr(df)                     → float  (put-call ratio by OI)
    max_pain(df)                → int    (strike causing max buyer pain)
    get_signal(df, spot, mode)  → dict   (direction + reason + key metrics)
"""

from __future__ import annotations

import threading
import time
from typing import Optional

import numpy as np
import pandas as pd
import requests

# ── NSE endpoints ──────────────────────────────────────────────────────────────
_BASE    = "https://www.nseindia.com"
_API_IDX = f"{_BASE}/api/option-chain-indices?symbol="
_API_EQT = f"{_BASE}/api/option-chain-equities?symbol="

_HEADERS = {
    "User-Agent":      (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept":          "application/json, text/plain, */*",
    "Accept-Language": "en-US,en;q=0.9",
    "Accept-Encoding": "gzip, deflate, br",
    "Referer":         "https://www.nseindia.com/option-chain",
    "Connection":      "keep-alive",
}

# Symbols that use the indices endpoint; everything else uses equities
_INDEX_SYMBOLS = {"NIFTY", "BANKNIFTY", "FINNIFTY", "MIDCPNIFTY", "NIFTYIT"}

# ── Thread-safe caches ─────────────────────────────────────────────────────────
_lock          = threading.Lock()
_sess:    Optional[requests.Session] = None
_sess_ts: float = 0.0
_SESSION_TTL   = 300   # seconds — refresh cookie every 5 min

_chain_cache: dict[str, tuple[dict, float]] = {}  # symbol → (data, ts)
_CHAIN_TTL   = 60   # seconds


# ── Internal helpers ───────────────────────────────────────────────────────────

def _get_session() -> requests.Session:
    global _sess, _sess_ts
    with _lock:
        now = time.time()
        if _sess is None or (now - _sess_ts) > _SESSION_TTL:
            s = requests.Session()
            try:
                s.get(_BASE, headers=_HEADERS, timeout=8)
            except Exception:
                pass
            _sess, _sess_ts = s, now
        return _sess


def _api_url(symbol: str) -> str:
    return _API_IDX + symbol if symbol.upper() in _INDEX_SYMBOLS else _API_EQT + symbol


# ── Public functions ───────────────────────────────────────────────────────────

def fetch_chain(symbol: str = "NIFTY") -> dict:
    """
    Return raw NSE option-chain JSON dict.
    Returns {} on any network / parse error (caller handles gracefully).
    Results cached for _CHAIN_TTL seconds.
    """
    sym = symbol.upper()
    now = time.time()
    with _lock:
        cached = _chain_cache.get(sym)
        if cached and (now - cached[1]) < _CHAIN_TTL:
            return cached[0]

    try:
        s = _get_session()
        r = s.get(_api_url(sym), headers=_HEADERS, timeout=10)
        r.raise_for_status()
        data = r.json()
        with _lock:
            _chain_cache[sym] = (data, time.time())
        return data
    except Exception:
        return {}


def get_expiries(symbol: str = "NIFTY") -> list[str]:
    """Return sorted list of available expiry date strings for nearest expiries."""
    raw  = fetch_chain(symbol)
    data = raw.get("records", {}).get("data", [])
    return sorted({d.get("expiryDate", "") for d in data if d.get("expiryDate")})


def parse_chain(symbol: str = "NIFTY", expiry: Optional[str] = None) -> pd.DataFrame:
    """
    Parse the raw chain into a tidy DataFrame.

    Columns
    -------
    strike | ce_oi | ce_oi_chg | ce_ltp | ce_iv | pe_oi | pe_oi_chg | pe_ltp | pe_iv
    """
    raw  = fetch_chain(symbol)
    data = raw.get("records", {}).get("data", [])
    if not data:
        return pd.DataFrame()

    if expiry is None:
        exps   = sorted({d.get("expiryDate", "") for d in data if d.get("expiryDate")})
        expiry = exps[0] if exps else None

    rows = []
    for d in data:
        if d.get("expiryDate") != expiry:
            continue
        ce = d.get("CE", {})
        pe = d.get("PE", {})
        rows.append({
            "strike":    d["strikePrice"],
            "ce_oi":     ce.get("openInterest", 0),
            "ce_oi_chg": ce.get("changeinOpenInterest", 0),
            "ce_ltp":    ce.get("lastPrice", 0),
            "ce_iv":     ce.get("impliedVolatility", 0),
            "pe_oi":     pe.get("openInterest", 0),
            "pe_oi_chg": pe.get("changeinOpenInterest", 0),
            "pe_ltp":    pe.get("lastPrice", 0),
            "pe_iv":     pe.get("impliedVolatility", 0),
        })

    if not rows:
        return pd.DataFrame()
    return pd.DataFrame(rows).sort_values("strike").reset_index(drop=True)


def pcr(df: pd.DataFrame) -> float:
    """Put-Call Ratio by total open interest."""
    if df.empty:
        return 1.0
    ce_sum = df["ce_oi"].sum()
    pe_sum = df["pe_oi"].sum()
    return round(pe_sum / ce_sum, 3) if ce_sum > 0 else 1.0


def max_pain(df: pd.DataFrame) -> int:
    """
    Max Pain strike — the strike at which total option-buyer payoff is minimised.
    Vectorised for speed: O(N²) but N is typically ≤ 200.
    """
    if df.empty:
        return 0
    strikes  = df["strike"].values.astype(float)
    ce_oi    = df["ce_oi"].values.astype(float)
    pe_oi    = df["pe_oi"].values.astype(float)
    # For each candidate expiry price s:  pain = Σ max(s-K,0)*ce_oi + Σ max(K-s,0)*pe_oi
    pain_arr = np.array([
        (np.maximum(s - strikes, 0) * ce_oi +
         np.maximum(strikes - s, 0) * pe_oi).sum()
        for s in strikes
    ])
    return int(strikes[np.argmin(pain_arr)])


def get_signal(df: pd.DataFrame, spot: float, mode: str = "PCR") -> dict:
    """
    Generate a directional trade signal from the options chain.

    Parameters
    ----------
    df   : DataFrame from parse_chain()
    spot : current index spot price
    mode : "PCR" | "MaxPain" | "OIBuildup"

    Returns
    -------
    direction  : "BUY_CE" | "BUY_PE" | "SELL_STRADDLE" | "WAIT"
    reason     : human-readable explanation
    pcr_val    : float
    max_pain_k : int
    atm_strike : int
    """
    empty = {
        "direction": "WAIT", "reason": "No chain data",
        "pcr_val": 1.0, "max_pain_k": 0, "atm_strike": 0,
    }
    if df.empty or spot <= 0:
        return empty

    step      = 100 if spot > 35_000 else 50
    atm       = int(round(spot / step) * step)
    _pcr      = pcr(df)
    _mp       = max_pain(df)
    atm_rows  = df[df["strike"] == atm]
    ce_chg    = int(atm_rows["ce_oi_chg"].sum()) if not atm_rows.empty else 0
    pe_chg    = int(atm_rows["pe_oi_chg"].sum()) if not atm_rows.empty else 0

    direction = "WAIT"
    reason    = f"PCR={_pcr:.2f} MaxPain={_mp}"

    if mode == "PCR":
        if _pcr > 1.4:
            direction = "BUY_CE"
            reason    = f"Bullish: PCR={_pcr:.2f} (>1.4, heavy PE writing = support)"
        elif _pcr < 0.65:
            direction = "BUY_PE"
            reason    = f"Bearish: PCR={_pcr:.2f} (<0.65, heavy CE writing = resistance)"
        elif 0.85 < _pcr < 1.15:
            direction = "SELL_STRADDLE"
            reason    = f"Neutral PCR={_pcr:.2f}, near MaxPain={_mp}"
        else:
            reason = f"PCR={_pcr:.2f} — inconclusive, waiting"

    elif mode == "MaxPain":
        diff_pct = (spot - _mp) / spot * 100
        if diff_pct > 0.5:
            direction = "BUY_PE"
            reason    = f"Spot {spot:.0f} above MaxPain {_mp} by {diff_pct:.2f}% → pull-down expected"
        elif diff_pct < -0.5:
            direction = "BUY_CE"
            reason    = f"Spot {spot:.0f} below MaxPain {_mp} by {abs(diff_pct):.2f}% → bounce expected"
        else:
            reason = f"Spot near MaxPain ({_mp}), ±{abs(diff_pct):.2f}% — no edge"

    elif mode == "OIBuildup":
        threshold = 500  # minimum OI change to be considered significant
        if ce_chg < -threshold and pe_chg > threshold:
            direction = "BUY_CE"
            reason    = f"CE unwinding ({ce_chg:+,}) + PE building ({pe_chg:+,}) → bullish"
        elif pe_chg < -threshold and ce_chg > threshold:
            direction = "BUY_PE"
            reason    = f"PE unwinding ({pe_chg:+,}) + CE building ({ce_chg:+,}) → bearish"
        else:
            reason = f"OI: CE_chg={ce_chg:+,}, PE_chg={pe_chg:+,} — no clear buildup"

    return {
        "direction":  direction,
        "reason":     reason,
        "pcr_val":    _pcr,
        "max_pain_k": _mp,
        "atm_strike": atm,
    }
