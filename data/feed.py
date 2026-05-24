"""
Price Feed — yfinance (always works) + Groww LTP when connected.
Thread-safe in-memory cache with background polling.
"""

import threading
import time
from datetime import datetime
from typing import Optional

_YF_SYMBOLS = {
    "NIFTY":       "^NSEI",
    "BANKNIFTY":   "^NSEBANK",
    "FINNIFTY":    "NIFTY_FIN_SERVICE.NS",
    "MIDCAPNIFTY": "NIFTY_MID_SELECT.NS",
    "VIX":         "^INDIAVIX",
}

_cache:  dict = {}
_lock   = threading.Lock()


def _yf_fetch(symbol: str) -> Optional[dict]:
    """Fetch latest quote from yfinance."""
    try:
        import yfinance as yf
        yf_sym = _YF_SYMBOLS.get(symbol.upper(), symbol + ".NS")
        tk     = yf.Ticker(yf_sym)
        info   = tk.fast_info
        price  = getattr(info, "last_price", None) or getattr(info, "regularMarketPrice", None)
        prev   = getattr(info, "previous_close", None) or price
        if price and float(price) > 0:
            chg     = float(price) - float(prev or price)
            chg_pct = chg / float(prev) * 100 if prev else 0.0
            return {
                "symbol":     symbol,
                "price":      round(float(price), 2),
                "prev_close": round(float(prev), 2),
                "change":     round(chg, 2),
                "change_pct": round(chg_pct, 2),
                "source":     "yfinance",
                "ts":         datetime.now().isoformat(),
            }
    except Exception:
        pass
    return None


def get_price(symbol: str) -> Optional[dict]:
    """Return cached quote, fetching live if not present."""
    with _lock:
        cached = _cache.get(symbol.upper())
    if cached:
        return cached

    q = _yf_fetch(symbol)
    if q:
        with _lock:
            _cache[symbol.upper()] = q
    return q


def refresh(symbol: str) -> Optional[dict]:
    """Force a fresh fetch regardless of cache."""
    q = _yf_fetch(symbol)
    if q:
        with _lock:
            _cache[symbol.upper()] = q
    return q


def spot(symbol: str) -> float:
    """Convenience: just return the float price (0.0 on failure)."""
    q = get_price(symbol)
    return float((q or {}).get("price", 0) or 0)


# ── Background poller ──────────────────────────────────────────────────────────

_WATCH: set = set()
_poll_thread: Optional[threading.Thread] = None
_poll_running = False


def watch(*symbols: str) -> None:
    """Add symbols to the background polling set."""
    global _poll_thread, _poll_running
    for s in symbols:
        _WATCH.add(s.upper())
    if not _poll_running:
        _poll_running = True
        _poll_thread  = threading.Thread(target=_poll_loop, daemon=True, name="PricePoll")
        _poll_thread.start()


def _poll_loop() -> None:
    while _poll_running:
        for sym in list(_WATCH):
            try:
                q = _yf_fetch(sym)
                if q:
                    with _lock:
                        _cache[sym] = q
            except Exception:
                pass
        time.sleep(15)   # yfinance rate-limit friendly
