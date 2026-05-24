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
from engine.indicators import IndicatorEngine
from engine.pricer import RISK_FREE_RATE, black_scholes

# ── Constants ──────────────────────────────────────────────────────────────────

LOT_SIZES  = {"NIFTY": 75, "BANKNIFTY": 30, "FINNIFTY": 40, "MIDCAPNIFTY": 50}
STEP_SIZES = {"NIFTY": 50, "BANKNIFTY": 100, "FINNIFTY": 50, "MIDCAPNIFTY": 25}

STRATEGY_NAMES = ["Options Chain", "MTF", "Intraday"]

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

    def _dispatch(self, run: StrategyRun, now: datetime) -> None:
        if   run.name == "Options Chain": self._options_chain(run, now)
        elif run.name == "MTF":           self._mtf(run, now)
        elif run.name == "Intraday":      self._intraday(run, now)

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
        qty      = int(p.get("qty", 50))
        at_entry = _past(p.get("entry_time", "09:20"), now)
        at_exit  = _past(p.get("exit_time",  "15:10"), now)
        tgt_pct  = float(p.get("target_pct", 0.8)) / 100
        sl_pct   = float(p.get("sl_pct",  0.4))    / 100

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
