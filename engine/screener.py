"""
Smart Stock Screener
====================
Scans Nifty 50 / Nifty Next 50 / F&O universe periodically in a background
thread.  Detects market regime (VIX-based) and scores every stock using a
multi-factor model combining:

  Momentum score  — RSI strength, EMA alignment, BB position, volume surge,
                    MACD crossover, ADX trend strength
  Reversion score — RSI oversold/overbought, BB squeeze, pullback from high,
                    volume dry-up, EMA21 support

Returns ranked candidates so the bot can auto-create MTF / Intraday runs.
"""

import threading
import time
from datetime import datetime
from typing import Optional

from data import feed
from engine.indicators import (
    adx, atr, bollinger_bands, ema, macd, rsi, sma, volume_ratio,
)

# ── Stock universes ────────────────────────────────────────────────────────────

NIFTY50: list[str] = [
    "RELIANCE", "TCS", "HDFCBANK", "INFY", "ICICIBANK",
    "HINDUNILVR", "ITC", "SBIN", "BHARTIARTL", "KOTAKBANK",
    "LT", "AXISBANK", "BAJFINANCE", "ASIANPAINT", "MARUTI",
    "HCLTECH", "WIPRO", "ULTRACEMCO", "NESTLEIND", "POWERGRID",
    "NTPC", "TATAMOTORS", "SUNPHARMA", "TITAN", "ONGC",
    "JSWSTEEL", "TATASTEEL", "ADANIPORTS", "TECHM", "M&M",
    "BAJAJFINSV", "DRREDDY", "CIPLA", "HINDALCO", "COALINDIA",
    "DIVISLAB", "BPCL", "SHREECEM", "HEROMOTOCO", "GRASIM",
    "BRITANNIA", "EICHERMOT", "BAJAJ-AUTO", "UPL", "APOLLOHOSP",
    "INDUSINDBK", "TATACONSUM", "SBILIFE", "HDFCLIFE", "LTF",
]

NIFTY_NEXT50: list[str] = [
    "ADANIENT", "ADANIGREEN", "AMBUJACEM", "AUROPHARMA", "BANDHANBNK",
    "BERGEPAINT", "BIOCON", "BOSCHLTD", "CANBK", "CHOLAFIN",
    "COLPAL", "DABUR", "DLF", "GAIL", "GODREJCP",
    "HAVELLS", "ICICIPRULI", "ICICIGI", "IDEA", "INDUSTOWER",
    "IRCTC", "JINDALSTEL", "JUBLFOOD", "LUPIN", "MARICO",
    "MOTHERSON", "MUTHOOTFIN", "NAUKRI", "OBEROIRLTY", "OFSS",
    "PAGEIND", "PERSISTENT", "PETRONET", "PIIND", "PIDILITIND",
    "POLYCAB", "PNB", "RECLTD", "SRF", "SIEMENS",
    "TORNTPHARM", "TRENT", "VBL", "VEDL", "VOLTAS",
    "WHIRLPOOL", "YESBANK", "ZOMATO", "ZYDUSLIFE", "MFSL",
]

# Frequently traded F&O stocks (additional beyond Nifty 100)
FNO_EXTRAS: list[str] = [
    "NIFTY", "BANKNIFTY", "FINNIFTY", "MIDCAPNIFTY",
    "ABB", "ABCAPITAL", "ABFRL", "ACC", "ALKEM",
    "BALKRISIND", "BANKBARODA", "BATAINDIA", "BEL", "BHEL",
    "CAMS", "CANFINHOME", "CONCOR", "COFORGE", "CROMPTON",
    "DEEPAKNTR", "DIXON", "ESCORTS", "GMRINFRA", "GNFC",
    "GRANULES", "GUJGASLTD", "HAL", "IDFCFIRSTB", "IGL",
    "INDIAMART", "INDUSTOWER", "INTELLECT", "IRFC", "LTIM",
    "LALPATHLAB", "LAURUSLABS", "M&MFIN", "MANAPPURAM", "MCX",
    "METROPOLIS", "MGL", "NAVINFLUOR", "NYKAA", "OBEROIRLTY",
    "PEL", "POLYCAB", "RAIN", "RAMCOCEM", "RBLBANK",
    "SAIL", "SUNTV", "TATACOMM", "TATACHEM", "TVSMOTOR",
]

# ── Regime thresholds ──────────────────────────────────────────────────────────

_VIX_LOW  = 14.0   # Low vol  → momentum / trend-following favoured
_VIX_HIGH = 22.0   # High vol → mean reversion / oversold bounces favoured

# ── Screener ───────────────────────────────────────────────────────────────────


class StockScreener:
    """
    Thread-safe screener.  Call ``screener.start()`` once; it scans every
    ``scan_interval`` seconds in the background.
    Access results via ``top_momentum()``, ``top_reversion()``, ``get_results()``.
    """

    def __init__(self) -> None:
        self._results:  dict               = {}
        self._breakout_results: dict       = {}
        self._bounce_results:   dict       = {}
        self._bulk_results:     dict       = {}
        self._lock      = threading.Lock()
        self._thread: Optional[threading.Thread] = None
        self._running   = False

        self.scan_interval: int   = 900    # 15 min between full scans
        self.universe: str        = "nifty50"   # nifty50 | nifty100 | fno | custom
        self.custom_symbols: list[str] = []
        self.last_scan: Optional[datetime] = None
        self.regime: str          = "NORMAL"
        self.min_price: float     = 100.0  # Skip sub-100 rupee stocks
        self.min_volume: int      = 100_000  # Skip illiquid stocks

    # ── Lifecycle ──────────────────────────────────────────────────────────────

    def start(self) -> None:
        if self._running:
            return
        self._running = True
        self._thread  = threading.Thread(target=self._loop, daemon=True, name="Screener")
        self._thread.start()

    def stop(self) -> None:
        self._running = False

    def _loop(self) -> None:
        while self._running:
            try:
                self.scan()
            except Exception:
                pass
            time.sleep(self.scan_interval)

    # ── Universe ───────────────────────────────────────────────────────────────

    def get_universe(self) -> list[str]:
        if   self.universe == "nifty50":  return list(NIFTY50)
        elif self.universe == "nifty100": return NIFTY50 + NIFTY_NEXT50
        elif self.universe == "fno":      return NIFTY50 + NIFTY_NEXT50 + FNO_EXTRAS
        elif self.universe == "custom":   return list(self.custom_symbols)
        return NIFTY50 + NIFTY_NEXT50

    # ── Regime detection ───────────────────────────────────────────────────────

    def detect_regime(self) -> str:
        vix = feed.spot("VIX")
        if vix <= 0:
            return "NORMAL"
        if   vix < _VIX_LOW:  return "TRENDING"   # Calm → follow trends
        elif vix > _VIX_HIGH: return "VOLATILE"   # Fear  → buy dips
        return "NORMAL"

    # ── Single-stock scoring ───────────────────────────────────────────────────

    def score_stock(self, symbol: str) -> Optional[dict]:
        """
        Download 60 days of daily OHLCV, compute indicators, return a scored dict.
        Returns None if data is unavailable or stock is too illiquid.
        """
        try:
            import yfinance as yf
            yf_sym = symbol + ".NS" if not symbol.startswith("^") else symbol
            hist   = yf.Ticker(yf_sym).history(period="90d", interval="1d")
            if hist.empty or len(hist) < 30:
                return None

            closes  = list(hist["Close"])
            highs   = list(hist["High"])
            lows    = list(hist["Low"])
            volumes = list(hist["Volume"])

            sp   = closes[-1]
            vol  = volumes[-1]

            if sp < self.min_price or vol < self.min_volume:
                return None

            # ── Indicators ────────────────────────────────────────────────────
            rsi14      = rsi(closes, 14)
            ema9       = ema(closes, 9)
            ema21      = ema(closes, 21)
            ema50      = ema(closes, 50) if len(closes) >= 50 else None
            bb_up, bb_mid, bb_low = bollinger_bands(closes, 20, 2.0)
            vol_r      = volume_ratio(volumes, 20)
            atr14      = atr(highs, lows, closes, 14)
            adx14      = adx(highs, lows, closes, 14)
            macd_l, macd_s, macd_h = macd(closes, 12, 26, 9)

            if rsi14 is None or ema9 is None or ema21 is None:
                return None

            # ── Momentum score (0-100) ────────────────────────────────────────
            mom = 0

            # RSI in sweet spot 55-72 = strong uptrend without extreme overbought
            if   55 <= rsi14 <= 72: mom += 20
            elif rsi14 > 72:        mom += 8    # Overbought — reduced
            elif 50 <= rsi14 < 55:  mom += 10

            # EMA alignment: price > EMA9 > EMA21 > EMA50 = perfect uptrend
            if ema50 and sp > ema9 > ema21 > ema50:   mom += 30
            elif sp > ema9 > ema21:                    mom += 20
            elif sp > ema21:                           mom += 8

            # Bollinger: price above midband = bullish
            if bb_up and bb_low and bb_up != bb_low:
                band_pos = (sp - bb_low) / (bb_up - bb_low)
                if band_pos > 0.8:   mom += 15   # Near upper band = breakout
                elif band_pos > 0.6: mom += 8

            # Volume surge = institutional participation
            if vol_r:
                if   vol_r >= 2.0: mom += 20
                elif vol_r >= 1.5: mom += 14
                elif vol_r >= 1.2: mom += 7

            # ADX: trend strength (25+ = trending)
            if adx14:
                if   adx14 >= 40: mom += 15
                elif adx14 >= 25: mom += 10
                elif adx14 >= 20: mom += 4

            # MACD bullish crossover
            if macd_l and macd_s and macd_h:
                if macd_h > 0 and macd_l > 0: mom += 10   # Both positive
                elif macd_h > 0:               mom += 5    # Histogram turning positive

            # ── Reversion score (0-100) ───────────────────────────────────────
            rev = 0

            # RSI oversold = potential bounce
            if   rsi14 <= 25: rev += 35
            elif rsi14 <= 32: rev += 25
            elif rsi14 <= 40: rev += 15

            # Near lower Bollinger band = support zone
            if bb_up and bb_low and bb_up != bb_low:
                band_pos = (sp - bb_low) / (bb_up - bb_low)
                if   band_pos <= 0.1: rev += 30
                elif band_pos <= 0.25: rev += 18
                elif band_pos <= 0.4:  rev += 8

            # Price below EMA9 but above EMA21 = shallow pullback, still healthy trend
            if ema21 and sp < ema9 and sp > ema21: rev += 20

            # Volume dry-up at support = sellers exhausted
            if vol_r and vol_r <= 0.6: rev += 15

            # Pullback from 52-week high: 5-20% below = healthy correction
            high52 = max(closes[-252:]) if len(closes) >= 252 else max(closes)
            pct_from_high = (sp - high52) / high52 * 100 if high52 > 0 else 0
            if -20 <= pct_from_high <= -5: rev += 15

            # MACD below zero but turning up = early reversal
            if macd_l and macd_s and macd_h:
                if macd_l < 0 and macd_h > 0: rev += 10   # Histogram just turned positive

            # ── ATR-based risk metrics ────────────────────────────────────────
            atr_pct = round(atr14 / sp * 100, 2) if (atr14 and sp > 0) else 1.5
            bb_width = round((bb_up - bb_low) / bb_mid * 100, 2) \
                       if (bb_up and bb_low and bb_mid and bb_mid > 0) else 5.0

            mom = min(100, mom)
            rev = min(100, rev)

            regime   = self.detect_regime()
            fit_mtf  = mom >= 55 if regime == "TRENDING" else (
                       rev >= 45 if regime == "VOLATILE" else (mom >= 45 or rev >= 40))
            fit_intr = mom >= 50 and (vol_r or 0) >= 1.3  # Intraday needs volume

            return {
                "symbol":       symbol,
                "price":        round(sp, 2),
                "rsi":          rsi14,
                "ema9":         ema9,
                "ema21":        ema21,
                "adx":          adx14,
                "vol_ratio":    vol_r,
                "atr_pct":      atr_pct,
                "bb_width":     bb_width,
                "macd_hist":    macd_h,
                "mom_score":    mom,
                "rev_score":    rev,
                "composite":    round((mom + rev) / 2, 1),
                "pct_from_52h": round(pct_from_high, 1),
                "signal":       "MOMENTUM" if mom >= rev else "REVERSION",
                "fit_mtf":      fit_mtf,
                "fit_intraday": fit_intr,
                "regime":       regime,
            }
        except Exception:
            return None

    # ── Full scan ──────────────────────────────────────────────────────────────

    def scan(self) -> dict:
        """Scan the full universe.  Blocks until done (~30s for 50 stocks)."""
        regime  = self.detect_regime()
        self.regime = regime
        symbols = self.get_universe()
        results: dict = {}

        for sym in symbols:
            scored = self.score_stock(sym)
            if scored:
                results[sym] = scored

        with self._lock:
            self._results = results
        self.last_scan = datetime.now()
        return results

    # ── Access results ─────────────────────────────────────────────────────────

    def get_results(self) -> dict:
        with self._lock:
            return dict(self._results)

    def top_momentum(self, n: int = 5) -> list[dict]:
        with self._lock:
            r = self._results
        return sorted(r.values(), key=lambda x: x["mom_score"], reverse=True)[:n]

    def top_reversion(self, n: int = 5) -> list[dict]:
        with self._lock:
            r = self._results
        return sorted(r.values(), key=lambda x: x["rev_score"], reverse=True)[:n]

    def top_for_mtf(self, n: int = 5) -> list[dict]:
        with self._lock:
            r = self._results
        cands = [v for v in r.values() if v.get("fit_mtf")]
        return sorted(cands, key=lambda x: x["composite"], reverse=True)[:n]

    def top_for_intraday(self, n: int = 5) -> list[dict]:
        with self._lock:
            r = self._results
        cands = [v for v in r.values() if v.get("fit_intraday")]
        return sorted(cands, key=lambda x: x["mom_score"], reverse=True)[:n]

    # ── Strategy-specific scanners ─────────────────────────────────────────────

    def score_breakout(self, symbol: str) -> "Optional[dict]":
        """
        BB squeeze → expansion breakout with price breaking 20-bar high + vol surge.
        Returns a scored dict only when all three conditions align.
        """
        try:
            import numpy as np
            from numpy.lib.stride_tricks import sliding_window_view
            import yfinance as yf
            yf_sym = symbol + ".NS"
            hist   = yf.Ticker(yf_sym).history(period="90d", interval="1d")
            if hist.empty or len(hist) < 30:
                return None
            closes  = list(hist["Close"])
            highs   = list(hist["High"])
            lows    = list(hist["Low"])
            volumes = list(hist["Volume"])
            sp  = closes[-1]
            vol = volumes[-1]
            if sp < self.min_price or vol < self.min_volume:
                return None

            # Vectorized O(n) BB width series using sliding windows
            c_arr = np.array(closes, dtype=float)
            period = 20
            if len(c_arr) >= period:
                wins   = sliding_window_view(c_arr, period)     # shape (n-19, 20)
                mu     = wins.mean(axis=1)
                std    = wins.std(axis=1, ddof=0)
                with np.errstate(divide="ignore", invalid="ignore"):
                    widths_arr = np.where(mu > 0, (4.0 * std / mu) * 100, 0.0)
                widths = widths_arr.tolist()
            else:
                return None

            if len(widths) < 10:
                return None
            recent_w = widths[-60:] if len(widths) >= 60 else widths
            p20_w    = sorted(recent_w)[max(0, int(len(recent_w) * 0.2) - 1)]
            was_squeezed = widths[-1] <= p20_w * 1.25

            # Price breakout: today's close above the highest of previous 20 bars
            high20 = max(highs[-21:-1])
            price_breakout = sp > high20 * 1.001

            vol_r  = volume_ratio(volumes, 20)
            vol_surge = vol_r is not None and vol_r >= 1.5

            rsi14 = rsi(closes, 14)
            rsi_ok = rsi14 is not None and rsi14 < 75

            if not (price_breakout and vol_surge and rsi_ok):
                return None

            score = 0
            if was_squeezed:                 score += 35
            if price_breakout:               score += 25
            if vol_r and vol_r >= 2.0:       score += 25
            elif vol_r and vol_r >= 1.5:     score += 15
            if rsi14 and 55 <= rsi14 <= 72:  score += 10
            adx14 = adx(highs, lows, closes, 14)
            if adx14 and adx14 >= 25:        score += 10

            atr14   = atr(highs, lows, closes, 14)
            atr_pct = round(atr14 / sp * 100, 2) if (atr14 and sp > 0) else 1.5
            sl_px   = round(high20 * 0.995, 2)
            tgt_px  = round(sp + (sp - sl_px) * 2.0, 2)
            return {
                "symbol":       symbol,
                "price":        round(sp, 2),
                "rsi":          rsi14,
                "adx":          adx14,
                "vol_ratio":    vol_r,
                "atr_pct":      atr_pct,
                "bb_width":     round(widths[-1], 2),
                "score":        min(100, score),
                "strategy":     "BREAKOUT",
                "direction":    "LONG",
                "entry":        sp,
                "sl":           sl_px,
                "target":       tgt_px,
                "was_squeezed": was_squeezed,
                "high20":       round(high20, 2),
            }
        except Exception:
            return None

    def score_bounce(self, symbol: str) -> "Optional[dict]":
        """
        RSI oversold + price at BB lower band + volume dry-up → mean reversion bounce.
        """
        try:
            import yfinance as yf
            yf_sym = symbol + ".NS"
            hist   = yf.Ticker(yf_sym).history(period="90d", interval="1d")
            if hist.empty or len(hist) < 30:
                return None
            closes  = list(hist["Close"])
            highs   = list(hist["High"])
            lows    = list(hist["Low"])
            volumes = list(hist["Volume"])
            sp  = closes[-1]
            vol = volumes[-1]
            if sp < self.min_price or vol < self.min_volume:
                return None

            rsi14              = rsi(closes, 14)
            bb_up, bb_mid, bb_low = bollinger_bands(closes, 20, 2.0)
            vol_r              = volume_ratio(volumes, 20)
            atr14              = atr(highs, lows, closes, 14)
            adx14              = adx(highs, lows, closes, 14)

            if rsi14 is None or bb_low is None or atr14 is None:
                return None

            rsi_oversold = rsi14 <= 40
            at_bb_lower  = sp <= bb_low * 1.02
            vol_dry      = vol_r is not None and vol_r <= 0.85

            if not (rsi_oversold and at_bb_lower):
                return None

            score = 0
            if   rsi14 <= 25: score += 35
            elif rsi14 <= 32: score += 25
            elif rsi14 <= 40: score += 15
            if at_bb_lower:  score += 30
            if vol_dry:      score += 20
            # Check if RSI is turning up (early reversal)
            prev_rsi = rsi(closes[:-1], 14) if len(closes) > 15 else None
            if prev_rsi is not None and rsi14 > prev_rsi:
                score += 15

            high52      = max(closes[-252:]) if len(closes) >= 252 else max(closes)
            pct_h52     = (sp - high52) / high52 * 100 if high52 > 0 else 0
            if -30 <= pct_h52 <= -5:
                score += 10

            if score < 30:
                return None

            atr_pct = round(atr14 / sp * 100, 2) if sp > 0 else 1.5
            sl_px   = round(sp - atr14 * 1.5, 2)
            tgt_px  = round(bb_mid, 2) if bb_mid else round(sp * (1 + atr_pct / 100 * 2), 2)
            bb_w    = round((bb_up - bb_low) / bb_mid * 100, 2) if (bb_mid and bb_mid > 0) else 5.0
            return {
                "symbol":       symbol,
                "price":        round(sp, 2),
                "rsi":          rsi14,
                "adx":          adx14,
                "vol_ratio":    vol_r,
                "atr_pct":      atr_pct,
                "bb_width":     bb_w,
                "score":        min(100, score),
                "strategy":     "BOUNCE",
                "direction":    "LONG",
                "entry":        sp,
                "sl":           sl_px,
                "target":       tgt_px,
                "bb_low":       round(bb_low, 2),
                "bb_mid":       round(bb_mid, 2),
                "pct_from_52h": round(pct_h52, 1),
            }
        except Exception:
            return None

    def score_bulk(self, symbol: str) -> "Optional[dict]":
        """
        Detect 5-min institutional bulk order via unusual volume spike.
        Returns signal only if current 5-min bar's volume ≥ bulk_ratio × 20-bar avg.
        """
        try:
            import yfinance as yf
            yf_sym = symbol + ".NS"
            h5     = yf.Ticker(yf_sym).history(period="5d", interval="5m")
            if h5.empty or len(h5) < 22:
                return None
            c5 = list(h5["Close"])
            v5 = list(h5["Volume"])
            sp     = c5[-1]
            avg_v  = sum(v5[-21:-1]) / 20
            if avg_v <= 0:
                return None
            bulk_r = round(v5[-1] / avg_v, 2)
            if bulk_r < 2.5:
                return None

            price_up  = c5[-1] > c5[-3] if len(c5) >= 3 else True
            direction = "LONG" if price_up else "SHORT"

            # Daily context
            h1d = yf.Ticker(yf_sym).history(period="30d", interval="1d")
            daily_rsi = rsi(list(h1d["Close"]), 14) if not h1d.empty and len(h1d) >= 15 else None
            daily_atr = atr(list(h1d["High"]), list(h1d["Low"]), list(h1d["Close"]), 14) \
                        if not h1d.empty and len(h1d) >= 15 else None

            score = 0
            if   bulk_r >= 5.0: score += 45
            elif bulk_r >= 3.5: score += 35
            elif bulk_r >= 2.5: score += 20
            if daily_rsi:
                if direction == "LONG"  and 40 <= daily_rsi <= 70: score += 25
                if direction == "SHORT" and 30 <= daily_rsi <= 60: score += 25
            if score < 30:
                return None

            atr_pct = round(daily_atr / sp * 100, 2) if (daily_atr and sp > 0) else 1.5
            sl_px   = round(sp * 0.996, 2) if direction == "LONG" else round(sp * 1.004, 2)
            tgt_px  = round(sp * (1 + atr_pct / 100 * 1.5), 2) if direction == "LONG" \
                      else round(sp * (1 - atr_pct / 100 * 1.5), 2)
            return {
                "symbol":      symbol,
                "price":       round(sp, 2),
                "bulk_ratio":  bulk_r,
                "rsi":         daily_rsi,
                "adx":         None,
                "vol_ratio":   bulk_r,
                "atr_pct":     atr_pct,
                "bb_width":    0.0,
                "score":       min(100, score),
                "strategy":    "BULK_ORDER",
                "direction":   direction,
                "entry":       sp,
                "sl":          sl_px,
                "target":      tgt_px,
            }
        except Exception:
            return None

    def scan_breakout(self) -> dict:
        """Scan universe for breakout candidates."""
        results: dict = {}
        for sym in self.get_universe():
            r = self.score_breakout(sym)
            if r:
                results[sym] = r
        with self._lock:
            self._breakout_results = results
        return results

    def scan_bounce(self) -> dict:
        """Scan universe for bounce candidates."""
        results: dict = {}
        for sym in self.get_universe():
            r = self.score_bounce(sym)
            if r:
                results[sym] = r
        with self._lock:
            self._bounce_results = results
        return results

    def scan_bulk(self) -> dict:
        """Scan universe for institutional bulk order spikes (uses 5-min data)."""
        results: dict = {}
        for sym in self.get_universe():
            r = self.score_bulk(sym)
            if r:
                results[sym] = r
        with self._lock:
            self._bulk_results = results
        return results

    def top_breakout(self, n: int = 5) -> list[dict]:
        with self._lock:
            r = dict(self._breakout_results)
        return sorted(r.values(), key=lambda x: x["score"], reverse=True)[:n]

    def top_bounce(self, n: int = 5) -> list[dict]:
        with self._lock:
            r = dict(self._bounce_results)
        return sorted(r.values(), key=lambda x: x["score"], reverse=True)[:n]

    def top_bulk(self, n: int = 5) -> list[dict]:
        with self._lock:
            r = dict(self._bulk_results)
        return sorted(r.values(), key=lambda x: x["score"], reverse=True)[:n]

    def regime_label(self) -> str:
        labels = {
            "TRENDING": "📈 TRENDING  (VIX < 14 — momentum favoured)",
            "VOLATILE": "⚡ VOLATILE  (VIX > 22 — mean reversion favoured)",
            "NORMAL":   "↔️ NORMAL    (VIX 14-22 — both strategies work)",
        }
        return labels.get(self.regime, self.regime)


screener = StockScreener()
