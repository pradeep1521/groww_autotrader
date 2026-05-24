"""
AutoTrader Bot Engine
=====================
Runs configurable strategies in a daemon thread, placing orders via Groww API
(or paper mode when not connected / paper=True).

Strategies
----------
  Short Straddle  — Sell ATM CE + PE, exit at premium% profit/loss or time
  Iron Condor     — Sell ATM±wing, buy OTM protection; exit at target or time
  ORB Breakout    — Trade 15-min opening range breakout; SL at range midpoint
  EMA Crossover   — Golden/death cross of EMA(fast) vs EMA(slow)

Usage
-----
    from engine.bot import bot
    bot.start()
    sid = bot.add_run("Short Straddle", symbol="NIFTY", lots=1, paper=True)
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

STRATEGY_NAMES = ["Short Straddle", "Iron Condor", "ORB Breakout", "EMA Crossover"]

STRATEGY_DEFAULTS: dict[str, dict] = {
    "Short Straddle": {"entry_time": "09:20", "exit_time": "15:15",
                       "target_pct": 50, "sl_pct": 100},
    "Iron Condor":    {"entry_time": "09:20", "exit_time": "15:15",
                       "wing_width": 2, "target_pct": 50},
    "ORB Breakout":   {"orb_minutes": 15, "exit_time": "15:15",
                       "sl_buffer_pct": 0.3, "target_mult": 2.0},
    "EMA Crossover":  {"fast_ema": 9, "slow_ema": 21, "exit_time": "15:15"},
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
        if   run.name == "Short Straddle": self._short_straddle(run, now)
        elif run.name == "Iron Condor":    self._iron_condor(run, now)
        elif run.name == "ORB Breakout":   self._orb(run, now)
        elif run.name == "EMA Crossover":  self._ema_cross(run, now)

    # ── Strategy: Short Straddle ───────────────────────────────────────────────

    def _short_straddle(self, run: StrategyRun, now: datetime) -> None:
        p   = run.params
        pe  = _past(p.get("entry_time", "09:20"), now)
        px  = _past(p.get("exit_time",  "15:15"), now)
        tgt = float(p.get("target_pct", 50))  / 100
        sl  = float(p.get("sl_pct",    100))  / 100

        if run.state == "WAITING" and pe and not px:
            sp = feed.spot(run.symbol)
            if sp <= 0:
                run.emit("⚠️ No spot price — waiting"); return

            step   = STEP_SIZES.get(run.symbol, 50)
            K      = int(round(sp / step) * step)
            expiry = _next_thursday()
            T      = max(0.001, (expiry - date.today()).days / 365)
            iv     = max(feed.spot("VIX") or 15, 5) / 100
            qty    = run.lots * LOT_SIZES.get(run.symbol, 75)

            ce_px  = max(round(black_scholes(sp, K, T, iv, RISK_FREE_RATE, "CE"), 2), 0.5)
            pe_px  = max(round(black_scholes(sp, K, T, iv, RISK_FREE_RATE, "PE"), 2), 0.5)
            ce_sym = _opt_sym(run.symbol, expiry, K, "CE")
            pe_sym = _opt_sym(run.symbol, expiry, K, "PE")

            self._place(run, ce_sym, "SELL", qty, ce_px, "CE", K)
            self._place(run, pe_sym, "SELL", qty, pe_px, "PE", K)

            total_cr = (ce_px + pe_px) * qty
            run.entry_data = {"K": K, "total_credit": total_cr}
            run.state = "ACTIVE"
            run.emit(f"✅ ENTERED spot={sp:.0f} K={K} CE=₹{ce_px:.2f} PE=₹{pe_px:.2f} "
                     f"credit=₹{total_cr:,.0f}")

        elif run.state == "ACTIVE":
            if px:
                self._exit(run, "⏱ Time exit"); return
            self._refresh_pnl(run)
            cr = run.entry_data.get("total_credit", 1)
            if run.pnl >= cr * tgt:
                self._exit(run, f"🎯 Target {tgt*100:.0f}% hit")
            elif run.pnl <= -cr * sl:
                self._exit(run, f"🛑 SL {sl*100:.0f}% hit")

    # ── Strategy: Iron Condor ──────────────────────────────────────────────────

    def _iron_condor(self, run: StrategyRun, now: datetime) -> None:
        p   = run.params
        pe  = _past(p.get("entry_time", "09:20"), now)
        px  = _past(p.get("exit_time",  "15:15"), now)
        w   = int(p.get("wing_width", 2))
        tgt = float(p.get("target_pct", 50)) / 100

        if run.state == "WAITING" and pe and not px:
            sp = feed.spot(run.symbol)
            if sp <= 0:
                run.emit("⚠️ No spot — waiting"); return

            step   = STEP_SIZES.get(run.symbol, 50)
            K      = int(round(sp / step) * step)
            expiry = _next_thursday()
            T      = max(0.001, (expiry - date.today()).days / 365)
            iv     = max(feed.spot("VIX") or 15, 5) / 100
            qty    = run.lots * LOT_SIZES.get(run.symbol, 75)

            legs = [
                (K + w * step,     "CE", "SELL"),
                (K + w * 2 * step, "CE", "BUY"),
                (K - w * step,     "PE", "SELL"),
                (K - w * 2 * step, "PE", "BUY"),
            ]
            net = 0.0
            for strike, ot, side in legs:
                px_ = max(round(black_scholes(sp, strike, T, iv, RISK_FREE_RATE, ot), 2), 0.1)
                sym = _opt_sym(run.symbol, expiry, strike, ot)
                self._place(run, sym, side, qty, px_, ot, strike)
                net += px_ if side == "SELL" else -px_
                run.emit(f"  {side} {sym} @ ₹{px_:.2f}")

            run.entry_data = {"K": K, "net_credit": net, "total_credit": net * qty}
            run.state = "ACTIVE"
            run.emit(f"✅ Iron Condor | K={K} | net ₹{net:.2f} | max profit ₹{net*qty:,.0f}")

        elif run.state == "ACTIVE":
            if px:
                self._exit(run, "⏱ Time exit"); return
            self._refresh_pnl(run)
            cr = run.entry_data.get("total_credit", 1)
            if run.pnl >= cr * tgt:
                self._exit(run, f"🎯 Target {tgt*100:.0f}%")

    # ── Strategy: ORB Breakout ─────────────────────────────────────────────────

    def _orb(self, run: StrategyRun, now: datetime) -> None:
        p       = run.params
        mins    = int(p.get("orb_minutes", 15))
        px      = _past(p.get("exit_time", "15:15"), now)
        sl_buf  = float(p.get("sl_buffer_pct", 0.3)) / 100
        tgt_m   = float(p.get("target_mult", 2.0))

        open_   = now.replace(hour=9,  minute=15, second=0, microsecond=0)
        end_orb = now.replace(hour=9,  minute=15 + mins, second=0, microsecond=0)

        sp  = feed.spot(run.symbol)
        if sp <= 0:
            return
        IndicatorEngine.for_symbol(run.symbol).push(sp)

        # Build range
        if open_ <= now < end_orb:
            run.params["_h"] = max(run.params.get("_h", 0), sp)
            run.params["_l"] = min(run.params.get("_l", 9e9), sp)
            run.emit(f"ORB forming H={run.params['_h']:.0f} L={run.params['_l']:.0f}")
            return

        orb_h = run.params.get("_h", 0)
        orb_l = run.params.get("_l", 9e9)
        if orb_h <= 0 or orb_l >= 9e9:
            return

        rng = orb_h - orb_l
        qty = run.lots * LOT_SIZES.get(run.symbol, 75)

        if run.state == "WAITING" and now >= end_orb:
            if sp > orb_h * (1 + sl_buf):
                sl_  = orb_h - rng * 0.5
                tgt_ = sp + rng * tgt_m
                self._place(run, run.symbol, "BUY", qty, sp, "EQUITY", 0)
                run.legs[-1].update({"sl": sl_, "tgt": tgt_})
                run.state = "ACTIVE"
                run.emit(f"📈 BUY @ ₹{sp:.2f} | SL ₹{sl_:.2f} | Target ₹{tgt_:.2f}")

            elif sp < orb_l * (1 - sl_buf):
                sl_  = orb_l + rng * 0.5
                tgt_ = sp - rng * tgt_m
                self._place(run, run.symbol, "SELL", qty, sp, "EQUITY", 0)
                run.legs[-1].update({"sl": sl_, "tgt": tgt_})
                run.state = "ACTIVE"
                run.emit(f"📉 SELL @ ₹{sp:.2f} | SL ₹{sl_:.2f} | Target ₹{tgt_:.2f}")

        elif run.state == "ACTIVE":
            if px:
                self._exit(run, "⏱ Time exit"); return
            leg = run.legs[0] if run.legs else None
            if not leg:
                return
            sl_, tgt_ = leg.get("sl", 0), leg.get("tgt", 0)
            if leg["side"] == "BUY":
                run.pnl = (sp - leg["entry_px"]) * qty
                if sp <= sl_:         self._exit(run, f"🛑 SL ₹{sl_:.0f}")
                elif tgt_ and sp >= tgt_: self._exit(run, f"🎯 Target ₹{tgt_:.0f}")
            else:
                run.pnl = (leg["entry_px"] - sp) * qty
                if sp >= sl_:         self._exit(run, f"🛑 SL ₹{sl_:.0f}")
                elif tgt_ and sp <= tgt_: self._exit(run, f"🎯 Target ₹{tgt_:.0f}")

    # ── Strategy: EMA Crossover ────────────────────────────────────────────────

    def _ema_cross(self, run: StrategyRun, now: datetime) -> None:
        p      = run.params
        fast_n = int(p.get("fast_ema", 9))
        slow_n = int(p.get("slow_ema", 21))
        px     = _past(p.get("exit_time", "15:15"), now)

        sp = feed.spot(run.symbol)
        if sp <= 0:
            return

        ind = IndicatorEngine.for_symbol(run.symbol)
        ind.push(sp)
        fast, slow = ind.ema(fast_n), ind.ema(slow_n)

        if fast is None or slow is None:
            run.emit(f"Building EMA ({len(ind)}/{slow_n} bars)…"); return

        pf_, ps_ = run.params.get("_pf"), run.params.get("_ps")
        run.params["_pf"], run.params["_ps"] = fast, slow

        qty = run.lots * LOT_SIZES.get(run.symbol, 75)

        if run.state == "ACTIVE" and px:
            self._exit(run, "⏱ Time exit"); return

        if run.state == "ACTIVE":
            leg = run.legs[0] if run.legs else None
            if leg:
                run.pnl = (sp - leg["entry_px"]) * qty if leg["side"] == "BUY" \
                          else (leg["entry_px"] - sp) * qty
                if (leg["side"] == "BUY"  and fast < slow) or \
                   (leg["side"] == "SELL" and fast > slow):
                    self._exit(run, "↩️ EMA reversal")
                    run.state = "WAITING"
            return

        if pf_ is None or ps_ is None:
            return

        was_bull, is_bull = pf_ > ps_, fast > slow
        if not was_bull and is_bull:
            self._place(run, run.symbol, "BUY", qty, sp, "EQUITY", 0)
            run.state = "ACTIVE"
            run.emit(f"📈 Golden cross EMA{fast_n}={fast:.0f}>EMA{slow_n}={slow:.0f} | BUY @ ₹{sp:.2f}")
        elif was_bull and not is_bull:
            self._place(run, run.symbol, "SELL", qty, sp, "EQUITY", 0)
            run.state = "ACTIVE"
            run.emit(f"📉 Death cross EMA{fast_n}={fast:.0f}<EMA{slow_n}={slow:.0f} | SELL @ ₹{sp:.2f}")

    # ── Shared helpers ─────────────────────────────────────────────────────────

    def _place(self, run: StrategyRun, symbol: str, side: str, qty: int,
               price: float, opt_type: str, strike: int) -> None:
        seg     = "DERIVATIVES" if opt_type in ("CE", "PE") else "CASH"
        product = "NRML"        if opt_type in ("CE", "PE") else "MIS"
        mode    = "📄 Paper" if run.paper else "🔴 LIVE"

        if not run.paper:
            groww.market_order(symbol, side, qty, seg, product)

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
