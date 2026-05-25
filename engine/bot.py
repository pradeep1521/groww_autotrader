"""
AutoTrader Bot Engine
=====================
Runs strategies in a daemon thread, placing orders via Groww API
(or paper mode when not connected / paper=True).

Strategies
----------
  Options Chain — PCR / MaxPain / OIBuildup signal → BUY CE, BUY PE, or SELL STRADDLE
  MTF           — Margin Trading Facility: swing trade stocks with EMA cross / RSI bounce
  Intraday      — MIS cash trades: VWAP bounce, ORB breakout, or Momentum (EMA+RSI)

Usage
-----
    from engine.bot import bot
    bot.start()
    sid = bot.add_run("Options Chain", symbol="NIFTY", lots=1, paper=True)
    bot.stop()
"""

import threading
import time
import uuid
from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Optional

from broker.groww import connector as groww
from data import db, feed
from engine.indicators import (
    IndicatorEngine, atr, bollinger_bands, rsi, volume_ratio,
)
from engine.pricer import RISK_FREE_RATE, black_scholes
from engine.risk_guard import risk_guard
from engine.screener import screener

# ── Constants ──────────────────────────────────────────────────────────────────

LOT_SIZES  = {"NIFTY": 75, "BANKNIFTY": 30, "FINNIFTY": 40, "MIDCAPNIFTY": 50}
STEP_SIZES = {"NIFTY": 50, "BANKNIFTY": 100, "FINNIFTY": 50, "MIDCAPNIFTY": 25}

STRATEGY_NAMES = ["Options Chain", "MTF", "Intraday", "Breakout", "Bounce", "Bulk Order"]

STRATEGY_DEFAULTS: dict[str, dict] = {
    "Options Chain": {
        "symbol":     "NIFTY",
        "mode":       "PCR",          # PCR | MaxPain | OIBuildup
        "direction":  "AUTO",         # AUTO | BUY_CE | BUY_PE | SELL_STRADDLE
        "lots":       1,
        "entry_time": "09:30",
        "exit_time":  "15:00",
        "target_pct": 50,
        "sl_pct":     30,
    },
    "MTF": {
        "symbol":     "RELIANCE",
        "signal":     "EMA Cross",    # EMA Cross | RSI Bounce
        "fast_ema":   9,
        "slow_ema":   21,
        "rsi_level":  35,
        "qty":        10,
        "target_pct": 2.0,
        "sl_pct":     1.0,
        "max_days":   3,
    },
    "Intraday": {
        "symbol":     "RELIANCE",
        "mode":       "VWAP",         # VWAP | ORB | Momentum
        "qty":        50,
        "entry_time": "09:20",
        "exit_time":  "15:10",
        "target_pct": 0.8,
        "sl_pct":     0.4,
        "orb_minutes": 15,
        "fast_ema":   9,
        "slow_ema":   21,
    },
    "Breakout": {
        "symbol":     "RELIANCE",
        "qty":        50,
        "lookback":   20,          # N-bar high breakout level
        "vol_min":    1.5,         # Min vol ratio to confirm breakout
        "target_rr":  2.0,         # R:R for target (target = entry + rr × (entry - sl))
        "exit_time":  "15:10",
    },
    "Bounce": {
        "symbol":     "RELIANCE",
        "qty":        50,
        "rsi_level":  40,          # RSI must be at or below this
        "atr_sl_mult": 1.5,        # SL = entry - mult × ATR
        "target_rr":  2.0,         # R:R for target (target = BB midband)
        "exit_time":  "15:10",
    },
    "Bulk Order": {
        "symbol":     "RELIANCE",
        "qty":        100,
        "bulk_ratio": 2.5,         # Min 5-min vol spike multiplier
        "target_pct": 0.8,         # % target from entry
        "sl_pct":     0.4,         # % SL from entry
        "exit_time":  "15:10",
    },
}


# ── Helpers ────────────────────────────────────────────────────────────────────

def _next_thursday() -> date:
    today = date.today()
    days  = (3 - today.weekday()) % 7
    return today + __import__("datetime").timedelta(days=days or 7)


def _opt_sym(underlying: str, expiry: date, strike: int, opt_type: str) -> str:
    return f"{underlying}{expiry.strftime('%d%b%y').upper()}{strike}{opt_type}"


def _past(time_str: str, now: datetime) -> bool:
    h, m = map(int, time_str.split(":"))
    return (now.hour, now.minute) >= (h, m)


# ── Strategy run state ─────────────────────────────────────────────────────────

@dataclass
class StrategyRun:
    id:         str
    name:       str
    symbol:     str
    lots:       int
    paper:      bool
    params:     dict

    state:      str   = "WAITING"    # WAITING | ACTIVE | EXITING | DONE | ERROR
    pnl:        float = 0.0
    entry_data: dict  = field(default_factory=dict)
    legs:       list  = field(default_factory=list)  # each leg = dict
    log:        list  = field(default_factory=list)
    created_at: str   = ""

    def emit(self, msg: str) -> None:
        ts = datetime.now().strftime("%H:%M:%S")
        self.log.append({"ts": ts, "msg": msg})
        if len(self.log) > 500:
            self.log = self.log[-500:]


# ── Core engine ────────────────────────────────────────────────────────────────

class AutoTrader:
    """
    Thread-safe trading bot. One global instance manages all strategy runs.
    Start with bot.start(). Add strategies with bot.add_run(...).
    """

    def __init__(self) -> None:
        self._runs:    list[StrategyRun]    = []
        self._lock:    threading.Lock       = threading.Lock()
        self._thread:  Optional[threading.Thread] = None
        self._running: bool   = False
        self.daily_pnl:       float = 0.0
        self.max_daily_loss:  float = -10_000.0
        self.tick_secs:       int   = 5
        # Auto-screener
        self.auto_screener:   bool  = False   # If True, auto-creates runs from screener
        self.auto_max_runs:   int   = 3       # Max auto-created runs per scan
        self.auto_paper:      bool  = True    # Auto runs always paper until explicitly disabled
        self._last_auto_scan: float = 0.0

    # ── Lifecycle ──────────────────────────────────────────────────────────────

    @property
    def is_running(self) -> bool:
        return self._running

    def start(self) -> None:
        if self._running:
            return
        self._running = True
        self._thread  = threading.Thread(target=self._loop, daemon=True, name="AutoTrader")
        self._thread.start()
        if self.auto_screener:
            screener.start()

    def stop(self) -> None:
        self._running = False

    def emergency_stop(self) -> None:
        self._running = False
        with self._lock:
            for run in self._runs:
                if run.state == "ACTIVE":
                    self._exit(run, "🚨 Emergency stop")

    # ── Run management ─────────────────────────────────────────────────────────

    def add_run(self, name: str, symbol: str, lots: int = 1,
                paper: bool = True, params: Optional[dict] = None) -> str:
        merged = {**STRATEGY_DEFAULTS.get(name, {}), **(params or {})}
        sid    = uuid.uuid4().hex[:8].upper()
        run    = StrategyRun(id=sid, name=name, symbol=symbol, lots=lots,
                             paper=paper, params=merged,
                             created_at=datetime.now().strftime("%H:%M:%S"))
        # Start background price polling
        feed.watch(symbol, "VIX")
        with self._lock:
            self._runs.append(run)
        return sid

    def remove_run(self, sid: str) -> None:
        with self._lock:
            self._runs = [r for r in self._runs if r.id != sid]

    def get_runs(self) -> list[StrategyRun]:
        with self._lock:
            return list(self._runs)

    def clear_done(self) -> None:
        with self._lock:
            self._runs = [r for r in self._runs if r.state not in ("DONE", "ERROR")]

    # ── Main loop ──────────────────────────────────────────────────────────────

    def _loop(self) -> None:
        while self._running:
            try:
                self._tick_all()
            except Exception:
                pass
            time.sleep(self.tick_secs)

    def _tick_all(self) -> None:
        if self.daily_pnl <= self.max_daily_loss:
            self.emergency_stop()
            return
        # Auto-screener: if enabled and screener has results, add top candidates
        if self.auto_screener:
            now_ts = time.time()
            if now_ts - self._last_auto_scan > screener.scan_interval:
                self.scan_and_auto_add()
                self._last_auto_scan = now_ts
        with self._lock:
            runs = list(self._runs)
        now = datetime.now()
        for run in runs:
            if run.state in ("DONE", "ERROR"):
                continue
            try:
                self._dispatch(run, now)
            except Exception as exc:
                run.state = "ERROR"
                run.emit(f"❌ Error: {exc}")

    def scan_and_auto_add(self) -> list[str]:
        """
        Trigger a screener scan and auto-create MTF+Intraday runs for top picks.
        Returns list of added run IDs.
        Only adds if we are below max_open_trades and no existing run for that symbol.
        """
        screener.scan()
        added: list[str] = []
        vix     = feed.spot("VIX") or 15
        regime  = screener.regime
        active  = sum(1 for r in self.get_runs() if r.state in ("WAITING", "ACTIVE"))
        max_add = max(0, self.auto_max_runs - active)
        if max_add == 0:
            return added

        existing_syms = {r.symbol for r in self.get_runs()
                         if r.state in ("WAITING", "ACTIVE")}

        # Pick top momentum for Intraday, top reversion/composite for MTF,
        # top breakout and bounce for the new strategies
        intraday_picks = [s for s in screener.top_for_intraday(5)
                          if s["symbol"] not in existing_syms]
        mtf_picks      = [s for s in screener.top_for_mtf(5)
                          if s["symbol"] not in existing_syms]
        breakout_picks = [s for s in screener.top_breakout(3)
                          if s["symbol"] not in existing_syms]
        bounce_picks   = [s for s in screener.top_bounce(3)
                          if s["symbol"] not in existing_syms]

        seen: set[str] = set()
        candidates: list[tuple[str, str]] = []   # (symbol, strategy)

        # Round-robin: Intraday → MTF → Breakout → Bounce
        pools = [
            (intraday_picks, "Intraday"),
            (mtf_picks,      "MTF"),
            (breakout_picks, "Breakout"),
            (bounce_picks,   "Bounce"),
        ]
        pool_idx = 0
        while len(candidates) < max_add:
            added_this_round = False
            for picks, strat in pools:
                if len(candidates) >= max_add:
                    break
                if picks:
                    s = picks.pop(0)
                    if s["symbol"] not in seen:
                        candidates.append((s["symbol"], strat))
                        seen.add(s["symbol"])
                        added_this_round = True
            if not added_this_round:
                break

        for sym, strat in candidates:
            scored = screener.get_results().get(sym, {})
            atr_pct  = scored.get("atr_pct", 1.5)
            sp       = scored.get("price", feed.spot(sym))

            if strat == "MTF":
                signal = "EMA Cross" if regime in ("TRENDING", "NORMAL") else "RSI Bounce"
                sl_pct  = round(risk_guard.vix_adjusted_sl_mult(vix) * atr_pct, 2)
                tgt_pct = round(sl_pct * 1.5, 2)
                qty     = risk_guard.position_size(sp, sp * atr_pct / 100, vix, active)
                sid = self.add_run("MTF", symbol=sym, paper=self.auto_paper, params={
                    "symbol": sym, "signal": signal,
                    "fast_ema": 9, "slow_ema": 21, "rsi_level": 35,
                    "qty": qty, "target_pct": tgt_pct, "sl_pct": sl_pct,
                    "max_days": 3, "auto_added": True,
                })
            elif strat == "Intraday":
                mode    = "Momentum" if regime == "TRENDING" else "VWAP"
                sl_pct  = round(risk_guard.vix_adjusted_sl_mult(vix) * atr_pct * 0.5, 2)
                tgt_pct = round(sl_pct * 1.5, 2)
                qty     = risk_guard.position_size(sp, sp * atr_pct / 100, vix, active)
                sid = self.add_run("Intraday", symbol=sym, paper=self.auto_paper, params={
                    "symbol": sym, "mode": mode, "qty": qty,
                    "entry_time": "09:20", "exit_time": "15:10",
                    "target_pct": tgt_pct, "sl_pct": sl_pct,
                    "fast_ema": 9, "slow_ema": 21, "auto_added": True,
                })
            elif strat == "Breakout":
                qty = risk_guard.position_size(sp, sp * atr_pct / 100, vix, active)
                sid = self.add_run("Breakout", symbol=sym, paper=self.auto_paper, params={
                    "symbol": sym, "qty": qty, "lookback": 20, "vol_min": 1.5,
                    "target_rr": 2.0, "exit_time": "15:10", "auto_added": True,
                })
            elif strat == "Bounce":
                qty = risk_guard.position_size(sp, sp * atr_pct / 100, vix, active)
                sid = self.add_run("Bounce", symbol=sym, paper=self.auto_paper, params={
                    "symbol": sym, "qty": qty, "rsi_level": 40,
                    "atr_sl_mult": 1.5, "target_rr": 2.0,
                    "exit_time": "15:10", "auto_added": True,
                })
            else:
                continue
            added.append(sid)
            active += 1

        if not self.is_running and added:
            self.start()

        return added

    def _dispatch(self, run: StrategyRun, now: datetime) -> None:
        if   run.name == "Options Chain": self._options_chain(run, now)
        elif run.name == "MTF":           self._mtf(run, now)
        elif run.name == "Intraday":      self._intraday(run, now)
        elif run.name == "Breakout":      self._breakout(run, now)
        elif run.name == "Bounce":        self._bounce(run, now)
        elif run.name == "Bulk Order":    self._bulk_order(run, now)

    # ── Strategy 1: Options Chain ──────────────────────────────────────────────

    def _options_chain(self, run: StrategyRun, now: datetime) -> None:
        from engine.options_chain import get_expiries, get_signal, parse_chain

        p         = run.params
        tgt       = float(p.get("target_pct", 50)) / 100
        sl        = float(p.get("sl_pct",     30))  / 100
        at_exit   = _past(p.get("exit_time", "15:00"), now)
        at_entry  = _past(p.get("entry_time", "09:30"), now)

        sp = feed.spot(run.symbol)
        if sp <= 0:
            run.emit("⚠️ No spot price — waiting"); return

        if run.state == "WAITING":
            if not at_entry:
                return
            if at_exit:
                run.state = "DONE"; run.emit("Market closed before entry"); return

            direction = p.get("direction", "AUTO")
            if direction == "AUTO":
                df  = parse_chain(run.symbol)
                sig = get_signal(df, sp, mode=p.get("mode", "PCR"))
                direction = sig["direction"]
                run.emit(f"Signal: {sig['reason']}")
            else:
                sig = {"atm_strike": 0, "pcr_val": 1.0, "max_pain_k": 0}

            if direction == "WAIT":
                run.emit("No trade signal this tick — will retry"); return

            step   = STEP_SIZES.get(run.symbol, 50)
            atm    = sig.get("atm_strike") or int(round(sp / step) * step)
            expiry = _next_thursday()
            T      = max(0.001, (expiry - date.today()).days / 365)
            iv     = max(feed.spot("VIX") or 15, 5) / 100
            qty    = int(p.get("lots", 1)) * LOT_SIZES.get(run.symbol, 75)

            if direction == "BUY_CE":
                px_  = max(round(black_scholes(sp, atm, T, iv, RISK_FREE_RATE, "CE"), 2), 0.5)
                sym  = _opt_sym(run.symbol, expiry, atm, "CE")
                self._place(run, sym, "BUY", qty, px_, "CE", atm)
                run.entry_data = {"direction": "BUY_CE", "entry_premium": px_, "qty": qty}
                run.state = "ACTIVE"
                run.emit(f"📈 BUY CE {sym} @ ₹{px_:.2f} | IV={iv*100:.0f}% ATM={atm}")

            elif direction == "BUY_PE":
                px_  = max(round(black_scholes(sp, atm, T, iv, RISK_FREE_RATE, "PE"), 2), 0.5)
                sym  = _opt_sym(run.symbol, expiry, atm, "PE")
                self._place(run, sym, "BUY", qty, px_, "PE", atm)
                run.entry_data = {"direction": "BUY_PE", "entry_premium": px_, "qty": qty}
                run.state = "ACTIVE"
                run.emit(f"📉 BUY PE {sym} @ ₹{px_:.2f} | IV={iv*100:.0f}% ATM={atm}")

            elif direction == "SELL_STRADDLE":
                ce_px = max(round(black_scholes(sp, atm, T, iv, RISK_FREE_RATE, "CE"), 2), 0.5)
                pe_px = max(round(black_scholes(sp, atm, T, iv, RISK_FREE_RATE, "PE"), 2), 0.5)
                self._place(run, _opt_sym(run.symbol, expiry, atm, "CE"), "SELL", qty, ce_px, "CE", atm)
                self._place(run, _opt_sym(run.symbol, expiry, atm, "PE"), "SELL", qty, pe_px, "PE", atm)
                total_cr = (ce_px + pe_px) * qty
                run.entry_data = {"direction": "SELL_STRADDLE", "total_credit": total_cr, "qty": qty}
                run.state = "ACTIVE"
                run.emit(f"⚡ SELL STRADDLE ATM={atm} CE₹{ce_px:.2f}+PE₹{pe_px:.2f} credit=₹{total_cr:,.0f}")

        elif run.state == "ACTIVE":
            if at_exit:
                self._exit(run, "⏱ Time exit"); return
            self._refresh_pnl(run)
            ed  = run.entry_data
            qty = ed.get("qty", 1)
            d   = ed.get("direction", "")

            if "BUY" in d:
                ep  = ed.get("entry_premium", 1)
                if run.pnl >= ep * qty * tgt:
                    self._exit(run, f"🎯 Target {tgt*100:.0f}% of premium")
                elif run.pnl <= -(ep * qty * sl):
                    self._exit(run, f"🛑 SL {sl*100:.0f}% of premium")
            else:  # SELL_STRADDLE
                cr = ed.get("total_credit", 1)
                if run.pnl >= cr * tgt:
                    self._exit(run, f"🎯 Target {tgt*100:.0f}% of credit")
                elif run.pnl <= -(cr * sl):
                    self._exit(run, f"🛑 SL {sl*100:.0f}% of credit")

    # ── Strategy 2: MTF (Margin Trading Facility) ─────────────────────────────

    def _mtf(self, run: StrategyRun, now: datetime) -> None:
        """
        Swing trade using Groww's Margin Trading Facility (leveraged equity).
        Not intraday — can hold for up to max_days.
        When use_atr_risk=True, SL and target are set dynamically via RiskGuard.
        """
        p       = run.params
        sig_m   = p.get("signal", "EMA Cross")
        fast_n  = int(p.get("fast_ema", 9))
        slow_n  = int(p.get("slow_ema", 21))
        rsi_lvl = float(p.get("rsi_level", 35))
        qty     = int(p.get("qty", 10))
        tgt_pct = float(p.get("target_pct", 2.0)) / 100
        sl_pct  = float(p.get("sl_pct", 1.0))  / 100
        max_d   = int(p.get("max_days", 3))
        use_atr = p.get("use_atr_risk", False)

        sp = feed.spot(run.symbol)
        if sp <= 0:
            run.emit("⚠️ No price data"); return

        ind = IndicatorEngine.for_symbol(run.symbol)
        ind.push(sp)
        fast = ind.ema(fast_n)
        slow = ind.ema(slow_n)
        rsi  = ind.rsi(14)

        if run.state == "WAITING":
            if fast is None or slow is None:
                run.emit(f"Building indicators ({len(ind)}/{slow_n})…"); return

            pf = p.get("_pf")
            ps = p.get("_ps")
            p["_pf"], p["_ps"] = fast, slow

            if pf is None:
                return

            triggered = False
            if sig_m == "EMA Cross":
                was_bull = pf > ps
                is_bull  = fast > slow
                if not was_bull and is_bull:
                    triggered = True
                    run.entry_data["ema_dir"] = "LONG"
                    run.emit(f"📈 Golden cross EMA{fast_n}>{fast_n}: BUY")
                elif was_bull and not is_bull:
                    triggered = True
                    run.entry_data["ema_dir"] = "SHORT"
                    run.emit(f"📉 Death cross EMA{fast_n}<{slow_n}: SELL")

            elif sig_m == "RSI Bounce":
                p_rsi = p.get("_prev_rsi")
                p["_prev_rsi"] = rsi
                if p_rsi is not None and rsi is not None:
                    if p_rsi < rsi_lvl and rsi >= rsi_lvl and fast > slow:
                        triggered = True
                        run.entry_data["ema_dir"] = "LONG"
                        run.emit(f"📈 RSI bounce {p_rsi:.1f}→{rsi:.1f} (EMA bullish): BUY")
                    elif p_rsi > (100 - rsi_lvl) and rsi <= (100 - rsi_lvl) and fast < slow:
                        triggered = True
                        run.entry_data["ema_dir"] = "SHORT"
                        run.emit(f"📉 RSI reversal {p_rsi:.1f}→{rsi:.1f} (EMA bearish): SELL")

            if triggered:
                side = "BUY" if run.entry_data.get("ema_dir") == "LONG" else "SELL"
                # Use ATR-based risk if enabled
                if use_atr:
                    scored = screener.get_results().get(run.symbol, {})
                    atr_v  = (scored.get("atr_pct", 1.5) / 100) * sp
                    vix    = feed.spot("VIX") or 15
                    act_sl  = risk_guard.sl_pct(sp, atr_v, vix)
                    act_tgt = risk_guard.target_pct(sp, atr_v, vix)
                    act_qty = risk_guard.position_size(sp, atr_v, vix,
                                                       sum(1 for r in self.get_runs()
                                                           if r.state in ("WAITING","ACTIVE")))
                    sl_pct  = act_sl  / 100
                    tgt_pct = act_tgt / 100
                    qty     = act_qty
                    run.emit(f"🔬 ATR risk: SL={act_sl:.1f}% Tgt={act_tgt:.1f}% Qty={qty}")
                self._place(run, run.symbol, side, qty, sp, "MTF_EQUITY", 0, product="MTF")
                run.entry_data.update({
                    "entry_px": sp, "qty": qty, "entry_date": now.date().isoformat(),
                })
                run.state = "ACTIVE"
                run.emit(f"✅ MTF {side} {qty}×{run.symbol} @ ₹{sp:.2f} | "
                         f"target ₹{sp*(1+tgt_pct):.2f} SL ₹{sp*(1-sl_pct):.2f}")

        elif run.state == "ACTIVE":
            ed        = run.entry_data
            entry_px  = ed.get("entry_px", sp)
            ema_dir   = ed.get("ema_dir", "LONG")
            entry_d   = ed.get("entry_date", now.date().isoformat())
            held_days = (now.date() - date.fromisoformat(entry_d)).days

            if ema_dir == "LONG":
                run.pnl = (sp - entry_px) * qty
                sl_px   = entry_px * (1 - sl_pct)
                tgt_px  = entry_px * (1 + tgt_pct)
                if sp <= sl_px:
                    self._exit(run, f"🛑 SL ₹{sl_px:.2f} hit")
                elif sp >= tgt_px:
                    self._exit(run, f"🎯 Target ₹{tgt_px:.2f} hit")
                elif held_days >= max_d:
                    self._exit(run, f"📅 Max {max_d} days reached")
            else:
                run.pnl = (entry_px - sp) * qty
                sl_px   = entry_px * (1 + sl_pct)
                tgt_px  = entry_px * (1 - tgt_pct)
                if sp >= sl_px:
                    self._exit(run, f"🛑 SL ₹{sl_px:.2f} hit")
                elif sp <= tgt_px:
                    self._exit(run, f"🎯 Target ₹{tgt_px:.2f} hit")
                elif held_days >= max_d:
                    self._exit(run, f"📅 Max {max_d} days reached")

        sp = feed.spot(run.symbol)
        if sp <= 0:
            run.emit("⚠️ No price data"); return

        ind = IndicatorEngine.for_symbol(run.symbol)
        ind.push(sp)
        fast = ind.ema(fast_n)
        slow = ind.ema(slow_n)
        rsi  = ind.rsi(14)

        if run.state == "WAITING":
            if fast is None or slow is None:
                run.emit(f"Building indicators ({len(ind)}/{slow_n})…"); return

            pf = p.get("_pf")
            ps = p.get("_ps")
            p["_pf"], p["_ps"] = fast, slow

            if pf is None:
                return

            triggered = False
            if sig_m == "EMA Cross":
                was_bull = pf > ps
                is_bull  = fast > slow
                if not was_bull and is_bull:
                    triggered = True
                    run.entry_data["ema_dir"] = "LONG"
                    run.emit(f"📈 Golden cross EMA{fast_n}>{fast_n}: BUY")
                elif was_bull and not is_bull:
                    triggered = True
                    run.entry_data["ema_dir"] = "SHORT"
                    run.emit(f"📉 Death cross EMA{fast_n}<{slow_n}: SELL")

            elif sig_m == "RSI Bounce":
                p_rsi = p.get("_prev_rsi")
                p["_prev_rsi"] = rsi
                if p_rsi is not None and rsi is not None:
                    if p_rsi < rsi_lvl and rsi >= rsi_lvl and fast > slow:
                        triggered = True
                        run.entry_data["ema_dir"] = "LONG"
                        run.emit(f"📈 RSI bounce {p_rsi:.1f}→{rsi:.1f} (EMA bullish): BUY")
                    elif p_rsi > (100 - rsi_lvl) and rsi <= (100 - rsi_lvl) and fast < slow:
                        triggered = True
                        run.entry_data["ema_dir"] = "SHORT"
                        run.emit(f"📉 RSI reversal {p_rsi:.1f}→{rsi:.1f} (EMA bearish): SELL")

            if triggered:
                side = "BUY" if run.entry_data.get("ema_dir") == "LONG" else "SELL"
                self._place(run, run.symbol, side, qty, sp, "MTF_EQUITY", 0, product="MTF")
                run.entry_data.update({
                    "entry_px": sp, "qty": qty, "entry_date": now.date().isoformat(),
                })
                run.state = "ACTIVE"
                run.emit(f"✅ MTF {side} {qty}×{run.symbol} @ ₹{sp:.2f} | target ₹{sp*(1+tgt_pct):.2f}")

        elif run.state == "ACTIVE":
            ed        = run.entry_data
            entry_px  = ed.get("entry_px", sp)
            ema_dir   = ed.get("ema_dir", "LONG")
            entry_d   = ed.get("entry_date", now.date().isoformat())
            held_days = (now.date() - date.fromisoformat(entry_d)).days

            if ema_dir == "LONG":
                run.pnl = (sp - entry_px) * qty
                sl_px   = entry_px * (1 - sl_pct)
                tgt_px  = entry_px * (1 + tgt_pct)
                if sp <= sl_px:
                    self._exit(run, f"🛑 SL ₹{sl_px:.2f} hit")
                elif sp >= tgt_px:
                    self._exit(run, f"🎯 Target ₹{tgt_px:.2f} hit")
                elif held_days >= max_d:
                    self._exit(run, f"📅 Max {max_d} days reached")
            else:
                run.pnl = (entry_px - sp) * qty
                sl_px   = entry_px * (1 + sl_pct)
                tgt_px  = entry_px * (1 - tgt_pct)
                if sp >= sl_px:
                    self._exit(run, f"🛑 SL ₹{sl_px:.2f} hit")
                elif sp <= tgt_px:
                    self._exit(run, f"🎯 Target ₹{tgt_px:.2f} hit")
                elif held_days >= max_d:
                    self._exit(run, f"📅 Max {max_d} days reached")

    # ── Strategy 3: Intraday (VWAP / ORB / Momentum) ─────────────────────────

    def _intraday(self, run: StrategyRun, now: datetime) -> None:
        p        = run.params
        mode     = p.get("mode", "VWAP")
        use_atr  = p.get("use_atr_risk", False)
        at_entry = _past(p.get("entry_time", "09:20"), now)
        at_exit  = _past(p.get("exit_time",  "15:10"), now)

        # ATR-based dynamic sizing if enabled
        if use_atr and "qty" not in p.get("_atr_sized", {}):
            scored = screener.get_results().get(run.symbol, {})
            if scored:
                sp_now = scored.get("price", feed.spot(run.symbol))
                atr_v  = (scored.get("atr_pct", 1.5) / 100) * sp_now
                vix    = feed.spot("VIX") or 15
                p["qty"] = risk_guard.position_size(
                    sp_now, atr_v, vix,
                    sum(1 for r in self.get_runs() if r.state in ("WAITING","ACTIVE"))
                )
                p["sl_pct"]     = risk_guard.sl_pct(sp_now, atr_v, vix)
                p["target_pct"] = risk_guard.target_pct(sp_now, atr_v, vix)
                p["_atr_sized"] = {"done": True}
                run.emit(f"🔬 ATR risk: SL={p['sl_pct']:.1f}% Tgt={p['target_pct']:.1f}% "
                         f"Qty={p['qty']}")

        qty     = int(p.get("qty", 50))
        tgt_pct = float(p.get("target_pct", 0.8)) / 100
        sl_pct  = float(p.get("sl_pct",  0.4))    / 100

        sp = feed.spot(run.symbol)
        if sp <= 0:
            return

        ind = IndicatorEngine.for_symbol(run.symbol)
        ind.push(sp)

        # ── Mandatory time exit ─────────────────────────────────────────────
        if run.state == "ACTIVE" and at_exit:
            self._exit(run, "⏱ Intraday auto square-off"); return

        # ── Check P&L on active position ────────────────────────────────────
        if run.state == "ACTIVE":
            leg = run.legs[0] if run.legs else None
            if leg:
                run.pnl = (sp - leg["entry_px"]) * qty if leg["side"] == "BUY" \
                          else (leg["entry_px"] - sp) * qty
                sl_px  = leg["entry_px"] * ((1 - sl_pct)  if leg["side"] == "BUY" else (1 + sl_pct))
                tgt_px = leg["entry_px"] * ((1 + tgt_pct) if leg["side"] == "BUY" else (1 - tgt_pct))
                if leg["side"] == "BUY":
                    if sp <= sl_px:  self._exit(run, f"🛑 SL ₹{sl_px:.2f}"); return
                    if sp >= tgt_px: self._exit(run, f"🎯 Target ₹{tgt_px:.2f}"); return
                else:
                    if sp >= sl_px:  self._exit(run, f"🛑 SL ₹{sl_px:.2f}"); return
                    if sp <= tgt_px: self._exit(run, f"🎯 Target ₹{tgt_px:.2f}"); return
            return

        if not at_entry or at_exit:
            return

        # ── VWAP mode ───────────────────────────────────────────────────────
        if mode == "VWAP":
            vwap = ind.vwap()
            rsi  = ind.rsi(14)
            if vwap is None or rsi is None:
                run.emit(f"Building VWAP ({len(ind)} ticks)…"); return
            if sp < vwap * 0.998 and rsi < 45:
                self._place(run, run.symbol, "BUY", qty, sp, "MIS_EQUITY", 0, product="MIS")
                run.state = "ACTIVE"
                run.emit(f"📈 VWAP BUY: price ₹{sp:.2f} < VWAP ₹{vwap:.2f} | RSI={rsi:.1f}")
            elif sp > vwap * 1.002 and rsi > 55:
                self._place(run, run.symbol, "SELL", qty, sp, "MIS_EQUITY", 0, product="MIS")
                run.state = "ACTIVE"
                run.emit(f"📉 VWAP SELL: price ₹{sp:.2f} > VWAP ₹{vwap:.2f} | RSI={rsi:.1f}")

        # ── ORB mode ────────────────────────────────────────────────────────
        elif mode == "ORB":
            mins    = int(p.get("orb_minutes", 15))
            open_   = now.replace(hour=9, minute=15, second=0, microsecond=0)
            end_orb = now.replace(hour=9, minute=15 + mins, second=0, microsecond=0)

            if open_ <= now < end_orb:
                p["_h"] = max(p.get("_h", 0), sp)
                p["_l"] = min(p.get("_l", 9e9), sp)
                return

            orb_h = p.get("_h", 0)
            orb_l = p.get("_l", 9e9)
            if orb_h <= 0 or orb_l >= 9e9:
                return
            rng = orb_h - orb_l

            if sp > orb_h * 1.001:
                sl_  = orb_h - rng * 0.5
                tgt_ = sp + rng * float(p.get("target_mult", 2.0))
                self._place(run, run.symbol, "BUY", qty, sp, "MIS_EQUITY", 0, product="MIS")
                run.legs[-1].update({"sl": sl_, "tgt": tgt_})
                run.state = "ACTIVE"
                run.emit(f"📈 ORB BUY ₹{sp:.2f} H={orb_h} | SL ₹{sl_:.2f} tgt ₹{tgt_:.2f}")
            elif sp < orb_l * 0.999:
                sl_  = orb_l + rng * 0.5
                tgt_ = sp - rng * float(p.get("target_mult", 2.0))
                self._place(run, run.symbol, "SELL", qty, sp, "MIS_EQUITY", 0, product="MIS")
                run.legs[-1].update({"sl": sl_, "tgt": tgt_})
                run.state = "ACTIVE"
                run.emit(f"📉 ORB SELL ₹{sp:.2f} L={orb_l} | SL ₹{sl_:.2f} tgt ₹{tgt_:.2f}")

        # ── Momentum mode ───────────────────────────────────────────────────
        elif mode == "Momentum":
            fast_n = int(p.get("fast_ema", 9))
            slow_n = int(p.get("slow_ema", 21))
            fast   = ind.ema(fast_n)
            slow   = ind.ema(slow_n)
            rsi    = ind.rsi(14)
            if fast is None or slow is None or rsi is None:
                run.emit(f"Building indicators…"); return
            pf = p.get("_pf")
            ps = p.get("_ps")
            p["_pf"], p["_ps"] = fast, slow
            if pf is None:
                return
            if not (pf > ps) and (fast > slow) and rsi > 55:
                self._place(run, run.symbol, "BUY", qty, sp, "MIS_EQUITY", 0, product="MIS")
                run.state = "ACTIVE"
                run.emit(f"📈 Momentum BUY EMA{fast_n}={fast:.0f}>EMA{slow_n}={slow:.0f} RSI={rsi:.1f}")
            elif (pf > ps) and not (fast > slow) and rsi < 45:
                self._place(run, run.symbol, "SELL", qty, sp, "MIS_EQUITY", 0, product="MIS")
                run.state = "ACTIVE"
                run.emit(f"📉 Momentum SELL EMA{fast_n}={fast:.0f}<EMA{slow_n}={slow:.0f} RSI={rsi:.1f}")

    # ── Strategy 4: Breakout (BB squeeze + 20-bar high + vol surge) ──────────

    def _breakout(self, run: StrategyRun, now: datetime) -> None:
        """
        Buys when price breaks above N-bar high on a volume surge,
        ideally after a Bollinger Band squeeze. Holds until target / SL / time exit.
        """
        import yfinance as yf
        p         = run.params
        qty       = int(p.get("qty", 50))
        lookback  = int(p.get("lookback", 20))
        vol_min   = float(p.get("vol_min", 1.5))
        target_rr = float(p.get("target_rr", 2.0))
        at_exit   = _past(p.get("exit_time", "15:10"), now)

        sp = feed.spot(run.symbol)
        if sp <= 0:
            return

        if run.state == "ACTIVE":
            if at_exit:
                self._exit(run, "⏱ Time exit"); return
            leg = run.legs[0] if run.legs else None
            if leg:
                sl_px  = run.entry_data.get("sl_px",  leg["entry_px"] * 0.99)
                tgt_px = run.entry_data.get("tgt_px", leg["entry_px"] * 1.02)
                run.pnl = (sp - leg["entry_px"]) * qty
                if sp <= sl_px:  self._exit(run, f"🛑 SL ₹{sl_px:.2f}"); return
                if sp >= tgt_px: self._exit(run, f"🎯 Target ₹{tgt_px:.2f}"); return
            return

        if at_exit:
            run.state = "DONE"; run.emit("Market closed before entry"); return

        try:
            hist = yf.Ticker(run.symbol + ".NS").history(period="60d", interval="1d")
            if hist.empty or len(hist) < lookback + 2:
                run.emit("Waiting for daily data…"); return
            highs   = list(hist["High"])
            closes  = list(hist["Close"])
            lows    = list(hist["Low"])
            volumes = list(hist["Volume"])
        except Exception:
            return

        high_n  = max(highs[-(lookback + 1):-1])
        vol_r   = volume_ratio(volumes, min(20, len(volumes) - 1))
        rsi14   = rsi(closes, 14)

        if sp > high_n * 1.001 and (vol_r or 0) >= vol_min and (rsi14 or 100) < 75:
            atr14  = atr(highs, lows, closes, 14) or sp * 0.015
            sl_px  = round(high_n * 0.995, 2)
            tgt_px = round(sp + (sp - sl_px) * target_rr, 2)
            self._place(run, run.symbol, "BUY", qty, sp, "CNC_EQUITY", 0, product="CNC")
            run.entry_data.update({"sl_px": sl_px, "tgt_px": tgt_px, "entry_px": sp})
            run.state = "ACTIVE"
            run.emit(f"📈 BREAKOUT BUY ₹{sp:.2f} > H{lookback}=₹{high_n:.2f} "
                     f"vol×{vol_r:.1f} | SL ₹{sl_px:.2f} Tgt ₹{tgt_px:.2f}")
        else:
            run.emit(f"Monitoring ₹{sp:.2f} | H{lookback}=₹{high_n:.2f} "
                     f"vol×{(vol_r or 0):.1f} RSI={rsi14 or '—'}")

    # ── Strategy 5: Bounce (RSI oversold + BB lower + vol dry-up) ────────────

    def _bounce(self, run: StrategyRun, now: datetime) -> None:
        """
        Mean-reversion bounce: enters LONG when RSI is oversold and price is
        at or below the lower Bollinger Band, with volume drying up.
        Target = BB midband. SL = entry - 1.5 × ATR.
        """
        import yfinance as yf
        p          = run.params
        qty        = int(p.get("qty", 50))
        rsi_thresh = float(p.get("rsi_level", 40))
        atr_mult   = float(p.get("atr_sl_mult", 1.5))
        at_exit    = _past(p.get("exit_time", "15:10"), now)

        sp = feed.spot(run.symbol)
        if sp <= 0:
            return

        if run.state == "ACTIVE":
            if at_exit:
                self._exit(run, "⏱ Time exit"); return
            leg = run.legs[0] if run.legs else None
            if leg:
                sl_px  = run.entry_data.get("sl_px",  leg["entry_px"] * 0.985)
                tgt_px = run.entry_data.get("tgt_px", leg["entry_px"] * 1.02)
                run.pnl = (sp - leg["entry_px"]) * qty
                if sp <= sl_px:  self._exit(run, f"🛑 SL ₹{sl_px:.2f}"); return
                if sp >= tgt_px: self._exit(run, f"🎯 Target ₹{tgt_px:.2f}"); return
            return

        if at_exit:
            run.state = "DONE"; run.emit("Market closed before entry"); return

        try:
            hist = yf.Ticker(run.symbol + ".NS").history(period="60d", interval="1d")
            if hist.empty or len(hist) < 22:
                run.emit("Waiting for daily data…"); return
            closes  = list(hist["Close"])
            highs   = list(hist["High"])
            lows    = list(hist["Low"])
            volumes = list(hist["Volume"])
        except Exception:
            return

        rsi14              = rsi(closes, 14)
        bb_up, bb_mid, bb_low = bollinger_bands(closes, 20, 2.0)
        vol_r              = volume_ratio(volumes, 20)
        atr14              = atr(highs, lows, closes, 14)

        if rsi14 is None or bb_low is None or atr14 is None:
            run.emit("Building indicators…"); return

        rsi_ok = rsi14 <= rsi_thresh
        bb_ok  = sp <= bb_low * 1.02
        vol_ok = (vol_r or 1.0) <= 0.85

        if rsi_ok and bb_ok:
            sl_px  = round(sp - atr14 * atr_mult, 2)
            tgt_px = round(bb_mid, 2) if bb_mid else round(sp * 1.02, 2)
            self._place(run, run.symbol, "BUY", qty, sp, "CNC_EQUITY", 0, product="CNC")
            run.entry_data.update({"sl_px": sl_px, "tgt_px": tgt_px, "entry_px": sp})
            run.state = "ACTIVE"
            run.emit(f"📉→📈 BOUNCE BUY ₹{sp:.2f} | RSI={rsi14:.1f} "
                     f"BB_low=₹{bb_low:.2f} vol×{(vol_r or 0):.2f} "
                     f"| SL ₹{sl_px:.2f} Tgt ₹{tgt_px:.2f}")
        else:
            run.emit(f"Monitoring ₹{sp:.2f} | RSI={rsi14:.1f} BB_low=₹{bb_low:.2f} "
                     f"{'✓' if rsi_ok else '✗'}RSI {'✓' if bb_ok else '✗'}BB "
                     f"{'✓' if vol_ok else '—'}Vol")

    # ── Strategy 6: Bulk Order (5-min institutional volume spike) ────────────

    def _bulk_order(self, run: StrategyRun, now: datetime) -> None:
        """
        Detects a 5-minute bar with unusually high volume (bulk_ratio × avg).
        Enters in the direction of the price move. Fast intraday trade.
        """
        import yfinance as yf
        p           = run.params
        qty         = int(p.get("qty", 100))
        bulk_thresh = float(p.get("bulk_ratio", 2.5))
        tgt_pct     = float(p.get("target_pct", 0.8)) / 100
        sl_pct      = float(p.get("sl_pct",     0.4)) / 100
        at_exit     = _past(p.get("exit_time", "15:10"), now)

        sp = feed.spot(run.symbol)
        if sp <= 0:
            return

        if run.state == "ACTIVE":
            if at_exit:
                self._exit(run, "⏱ Time exit"); return
            leg = run.legs[0] if run.legs else None
            if leg:
                run.pnl = (sp - leg["entry_px"]) * qty if leg["side"] == "BUY" \
                          else (leg["entry_px"] - sp) * qty
                sl_px  = run.entry_data.get("sl_px",  0)
                tgt_px = run.entry_data.get("tgt_px", 0)
                if leg["side"] == "BUY":
                    if sp <= sl_px:  self._exit(run, f"🛑 SL ₹{sl_px:.2f}"); return
                    if sp >= tgt_px: self._exit(run, f"🎯 Tgt ₹{tgt_px:.2f}"); return
                else:
                    if sp >= sl_px:  self._exit(run, f"🛑 SL ₹{sl_px:.2f}"); return
                    if sp <= tgt_px: self._exit(run, f"🎯 Tgt ₹{tgt_px:.2f}"); return
            return

        if at_exit:
            run.state = "DONE"; run.emit("Market closed before entry"); return

        try:
            h5 = yf.Ticker(run.symbol + ".NS").history(period="5d", interval="5m")
            if h5.empty or len(h5) < 22:
                run.emit("Waiting for 5m data…"); return
            c5 = list(h5["Close"])
            v5 = list(h5["Volume"])
        except Exception:
            return

        avg_v  = sum(v5[-21:-1]) / 20
        bulk_r = (v5[-1] / avg_v) if avg_v > 0 else 0

        if bulk_r >= bulk_thresh:
            price_up = c5[-1] > c5[-3] if len(c5) >= 3 else True
            side     = "BUY" if price_up else "SELL"
            sl_px    = round(sp * (1 - sl_pct),  2) if side == "BUY" else round(sp * (1 + sl_pct),  2)
            tgt_px   = round(sp * (1 + tgt_pct), 2) if side == "BUY" else round(sp * (1 - tgt_pct), 2)
            self._place(run, run.symbol, side, qty, sp, "MIS_EQUITY", 0, product="MIS")
            run.entry_data.update({"sl_px": sl_px, "tgt_px": tgt_px})
            run.state = "ACTIVE"
            run.emit(f"🔵 BULK {side} ₹{sp:.2f} vol×{bulk_r:.1f}x "
                     f"| SL ₹{sl_px:.2f} Tgt ₹{tgt_px:.2f}")
        else:
            run.emit(f"Watching bulk: ×{bulk_r:.1f} (need ×{bulk_thresh:.1f})")

    # ── Shared helpers ─────────────────────────────────────────────────────────

    def _place(self, run: StrategyRun, symbol: str, side: str, qty: int,
               price: float, opt_type: str, strike: int,
               product: Optional[str] = None) -> None:
        seg     = "DERIVATIVES" if opt_type in ("CE", "PE") else "CASH"
        prod    = product or ("NRML" if opt_type in ("CE", "PE") else "MIS")
        mode    = "📄 Paper" if run.paper else "🔴 LIVE"

        if not run.paper:
            groww.market_order(symbol, side, qty, seg, prod)

        trade_id = db.open_trade(run.id, run.name, symbol, side, qty, price, run.paper)
        run.legs.append({
            "trade_id": trade_id,
            "sym":      symbol,
            "side":     side,
            "qty":      qty,
            "entry_px": price,
            "exit_px":  None,
            "pnl":      0.0,
            "type":     opt_type,
            "strike":   strike,
        })
        run.emit(f"{mode} {side} {qty}×{symbol} @ ₹{price:.2f}")

    def _refresh_pnl(self, run: StrategyRun) -> None:
        total = 0.0
        for leg in run.legs:
            if leg.get("exit_px") is not None:
                continue
            sp = feed.spot(leg["sym"])
            if sp <= 0:
                sp = leg["entry_px"]
            total += (leg["entry_px"] - sp) * leg["qty"] if leg["side"] == "SELL" \
                else  (sp - leg["entry_px"]) * leg["qty"]
        run.pnl = total

    def _exit(self, run: StrategyRun, reason: str = "") -> None:
        run.state = "EXITING"
        run.emit(f"EXIT: {reason}")
        total = 0.0
        for leg in run.legs:
            if leg.get("exit_px") is not None:
                continue
            sp      = feed.spot(leg["sym"]) or leg["entry_px"]
            seg     = "DERIVATIVES" if leg["type"] in ("CE", "PE") else "CASH"
            product = "NRML"        if leg["type"] in ("CE", "PE") else "MIS"
            if not run.paper:
                exit_side = "BUY" if leg["side"] == "SELL" else "SELL"
                groww.market_order(leg["sym"], exit_side, leg["qty"], seg, product)
            db.close_trade(leg["trade_id"], sp)
            pnl = (leg["entry_px"] - sp) * leg["qty"] if leg["side"] == "SELL" \
                  else (sp - leg["entry_px"]) * leg["qty"]
            leg["exit_px"] = sp
            leg["pnl"]     = pnl
            total         += pnl
            run.emit(f"  ↳ {leg['sym']} @ ₹{sp:.2f} | P&L ₹{pnl:+.0f}")
        run.pnl        = total
        run.state      = "DONE"
        self.daily_pnl += total
        run.emit(f"✅ DONE — Final P&L ₹{total:+,.0f}")


# ── Singleton ──────────────────────────────────────────────────────────────────
bot = AutoTrader()
