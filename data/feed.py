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
    """
    Fetch latest quote from yfinance.

    Uses history(period='5d') instead of fast_info to avoid yfinance
    internally triggering a 1-year download (which floods logs with
    'possibly delisted' warnings for Indian index tickers).
    """
    try:
        import logging
        import yfinance as yf

        # Suppress yfinance's noisy download warnings at the fetch level
        _yf_logger = logging.getLogger("yfinance")
        _prev_level = _yf_logger.level
        _yf_logger.setLevel(logging.CRITICAL)

        try:
            yf_sym = _YF_SYMBOLS.get(symbol.upper(), symbol.upper() + ".NS")
            hist   = yf.Ticker(yf_sym).history(period="5d", interval="1d", auto_adjust=True)
        finally:
            _yf_logger.setLevel(_prev_level)

        if hist.empty:
            return None
        price = float(hist["Close"].iloc[-1])
        prev  = float(hist["Close"].iloc[-2]) if len(hist) >= 2 else price
        if price <= 0:
            return None
        chg     = price - prev
        chg_pct = chg / prev * 100 if prev > 0 else 0.0
        return {
            "symbol":     symbol,
            "price":      round(price, 2),
            "prev_close": round(prev, 2),
            "change":     round(chg, 2),
            "change_pct": round(chg_pct, 2),
            "source":     "yfinance",
            "ts":         datetime.now().isoformat(),
        }
    except Exception:
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


def ohlcv(symbol: str, period: str = "1d", interval: str = "5m"):
    """
    Return a pandas DataFrame with columns [Open, High, Low, Close, Volume]
    for charting.  Uses yfinance history().

    period   — '1d','5d','1mo','3mo','6mo','1y','2y'
    interval — '1m','5m','15m','30m','1h','1d','1wk'
    """
    try:
        import yfinance as yf
        yf_sym = _YF_SYMBOLS.get(symbol.upper(), symbol.upper() + ".NS")
        df     = yf.Ticker(yf_sym).history(period=period, interval=interval)
        if df.empty:
            return None
        df.index = df.index.tz_localize(None) if df.index.tzinfo else df.index
        return df[["Open", "High", "Low", "Close", "Volume"]].copy()
    except Exception:
        return None


def batch_refresh(symbols: list) -> dict:
    """Force-refresh prices for a list of symbols. Returns {symbol: price_dict}."""
    results = {}
    for sym in symbols:
        q = _yf_fetch(sym)
        if q:
            with _lock:
                _cache[sym.upper()] = q
            results[sym.upper()] = q
    return results


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
