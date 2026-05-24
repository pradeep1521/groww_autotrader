"""
Strategy Manager — three focused tabs: Options Chain · MTF · Intraday
"""

import streamlit as st

from broker.groww import connector
from engine.bot import STRATEGY_DEFAULTS, bot

st.set_page_config(page_title="Strategies", page_icon="⚙️", layout="wide")
st.title("⚙️ Strategy Manager")

live_mode = connector.is_connected

tab_oc, tab_mtf, tab_intra, tab_runs = st.tabs(
    ["📊 Options Chain", "💳 MTF", "⚡ Intraday", "📋 Active Runs"]
)

# ══════════════════════════════════════════════════════════════════════════════
#  Tab 1 — Options Chain
# ══════════════════════════════════════════════════════════════════════════════
with tab_oc:
    st.subheader("📊 Nifty Options Chain Trading")
    st.caption(
        "Reads live NSE options chain → calculates PCR / MaxPain / OI Buildup "
        "→ auto-enters CE, PE, or Straddle on signal."
    )

    left, right = st.columns([5, 5], gap="large")
    with left:
        d = STRATEGY_DEFAULTS["Options Chain"]
        sym = st.selectbox("Index", ["NIFTY", "BANKNIFTY", "FINNIFTY"], key="oc_sym")
        a1, a2 = st.columns(2)
        mode = a1.selectbox("Signal mode", ["PCR", "MaxPain", "OIBuildup"], key="oc_mode",
                            help="PCR: Put-Call Ratio | MaxPain: price gravitates to max-pain strike | OIBuildup: fresh OI buildup/unwinding")
        direction = a2.selectbox("Direction override",
                                 ["AUTO", "BUY_CE", "BUY_PE", "SELL_STRADDLE"],
                                 key="oc_dir",
                                 help="AUTO lets the signal engine decide. Override to force a direction.")
        b1, b2 = st.columns(2)
        lots       = b1.number_input("Lots", 1, 50, 1, key="oc_lots")
        entry_time = b2.text_input("Entry time HH:MM", d["entry_time"], key="oc_et")
        c1, c2, c3 = st.columns(3)
        exit_time  = c1.text_input("Exit time HH:MM", d["exit_time"], key="oc_xt")
        target_pct = c2.number_input("Target % of premium", 10, 100, d["target_pct"], key="oc_tp")
        sl_pct     = c3.number_input("SL % of premium", 10, 100, d["sl_pct"], key="oc_sl")
        mode_str = "Paper 📄" if not live_mode else st.radio("Mode", ["Paper 📄", "Live 🔴"], horizontal=True, key="oc_m")
        paper = "Paper" in mode_str or not live_mode

        if st.button("🚀 Activate Options Chain", type="primary", use_container_width=True, key="oc_add"):
            sid = bot.add_run("Options Chain", symbol=sym, paper=paper, params={
                "symbol": sym, "mode": mode, "direction": direction, "lots": lots,
                "entry_time": entry_time, "exit_time": exit_time,
                "target_pct": target_pct, "sl_pct": sl_pct,
            })
            if not bot.is_running: bot.start()
            st.success(f"✅ Options Chain on **{sym}** added (ID `{sid}`)")

    with right:
        st.markdown("""
**How it works**

| Signal Mode | Entry Logic |
|---|---|
| **PCR** | PCR > 1.4 → BUY CE · PCR < 0.65 → BUY PE · PCR 0.85-1.15 → SELL STRADDLE |
| **MaxPain** | Spot > MaxPain+0.5% → BUY PE (pull down) · Spot < MaxPain-0.5% → BUY CE |
| **OIBuildup** | CE unwinding + PE building → BUY CE · PE unwinding + CE building → BUY PE |

**Exit logic** — whichever hits first:
- ✅ Premium decays by *Target%* (for buys)
- 🛑 Premium rises by *SL%* against position
- ⏱ Auto exit at configured exit time

**Sell Straddle** sells ATM CE + PE simultaneously.  
Max profit = full premium received. SL = 2× of received premium.
        """)

        # Live chain snapshot
        st.divider()
        if st.button("🔄 Load live chain snapshot", key="oc_preview"):
            from engine.options_chain import get_expiries, get_signal, parse_chain, pcr, max_pain
            from data import feed
            sp = feed.spot(sym)
            df = parse_chain(sym)
            if df.empty:
                st.warning("NSE API unavailable right now — chain data could not be fetched.")
            else:
                exps  = get_expiries(sym)
                _pcr  = pcr(df)
                _mp   = max_pain(df)
                sig   = get_signal(df, sp, mode=mode)
                m1, m2, m3, m4 = st.columns(4)
                m1.metric("Spot", f"₹{sp:,.0f}")
                m2.metric("PCR", f"{_pcr:.3f}")
                m3.metric("Max Pain", f"{_mp:,}")
                m4.metric("Signal", sig["direction"])
                st.info(f"💡 {sig['reason']}")
                step = 100 if sp > 35000 else 50
                atm  = int(round(sp / step) * step)
                near_df = df[(df["strike"] >= atm - step * 8) & (df["strike"] <= atm + step * 8)].copy()
                near_df = near_df[["strike", "ce_oi", "ce_oi_chg", "ce_ltp", "ce_iv",
                                   "pe_oi", "pe_oi_chg", "pe_ltp", "pe_iv"]]
                st.dataframe(near_df, use_container_width=True, hide_index=True)

# ══════════════════════════════════════════════════════════════════════════════
#  Tab 2 — MTF
# ══════════════════════════════════════════════════════════════════════════════
with tab_mtf:
    st.subheader("💳 MTF — Margin Trading Facility")
    st.caption(
        "Leveraged equity swing trades using Groww MTF. "
        "Hold for 1–5 days. Entry on EMA crossover or RSI oversold bounce."
    )

    left2, right2 = st.columns([5, 5], gap="large")
    with left2:
        d = STRATEGY_DEFAULTS["MTF"]
        sym2    = st.text_input("Stock symbol (NSE)", d["symbol"].upper(), key="mtf_sym",
                                help="Examples: RELIANCE, SBIN, TCS, INFY, HDFCBANK")
        signal  = st.selectbox("Entry signal", ["EMA Cross", "RSI Bounce"], key="mtf_sig")
        e1, e2, e3 = st.columns(3)
        fast_ema  = e1.number_input("Fast EMA", 3,  50, d["fast_ema"],   key="mtf_fe")
        slow_ema  = e2.number_input("Slow EMA", 10, 200, d["slow_ema"],  key="mtf_se")
        rsi_level = e3.number_input("RSI bounce level", 20, 50, d["rsi_level"], key="mtf_rsi",
                                    help="RSI Bounce mode only: enter when RSI crosses above this from below")
        f1, f2, f3, f4 = st.columns(4)
        qty2      = f1.number_input("Qty (shares)", 1, 10000, d["qty"],          key="mtf_qty")
        tgt2      = f2.number_input("Target %",     0.1, 20.0, d["target_pct"],  key="mtf_tp", step=0.1)
        sl2       = f3.number_input("SL %",         0.1, 10.0, d["sl_pct"],      key="mtf_sl", step=0.1)
        max_days  = f4.number_input("Max hold days", 1, 30, d["max_days"],        key="mtf_md")
        mode_str2 = "Paper 📄" if not live_mode else st.radio("Mode", ["Paper 📄", "Live 🔴"], horizontal=True, key="mtf_m")
        paper2    = "Paper" in mode_str2 or not live_mode

        if st.button("🚀 Activate MTF", type="primary", use_container_width=True, key="mtf_add"):
            sid = bot.add_run("MTF", symbol=sym2.upper(), paper=paper2, params={
                "symbol": sym2.upper(), "signal": signal, "fast_ema": fast_ema,
                "slow_ema": slow_ema, "rsi_level": rsi_level, "qty": qty2,
                "target_pct": tgt2, "sl_pct": sl2, "max_days": max_days,
            })
            if not bot.is_running: bot.start()
            st.success(f"✅ MTF on **{sym2.upper()}** added (ID `{sid}`)")

    with right2:
        st.markdown("""
**MTF (Margin Trading Facility)** lets you buy stocks with up to 4× leverage.
Positions can be held overnight — unlike intraday MIS orders.

| Signal | Trigger |
|---|---|
| **EMA Cross** | Golden cross (fast > slow) → BUY · Death cross → SELL |
| **RSI Bounce** | RSI crosses above *rsi_level* + EMA bullish → BUY · crosses below (100-level) + EMA bearish → SELL |

**Exit logic** — whichever hits first:
- ✅ Price moves *Target%* in your favour
- 🛑 Price moves *SL%* against you
- 📅 *Max hold days* reached (time-based exit)

> ⚠️ MTF carries overnight risk. Groww charges interest after day 1.
> Only available when **Groww is connected** in live mode.
        """)

# ══════════════════════════════════════════════════════════════════════════════
#  Tab 3 — Intraday
# ══════════════════════════════════════════════════════════════════════════════
with tab_intra:
    st.subheader("⚡ Intraday Trading (MIS)")
    st.caption(
        "Same-day MIS orders auto square-off at exit time. "
        "Three modes: VWAP bounce · ORB breakout · Momentum."
    )

    left3, right3 = st.columns([5, 5], gap="large")
    with left3:
        d = STRATEGY_DEFAULTS["Intraday"]
        sym3   = st.text_input("Symbol (NSE)", d["symbol"].upper(), key="int_sym",
                               help="Stock or ETF. E.g.: RELIANCE, NIFTYBEES, BANKBEES")
        mode3  = st.selectbox("Mode", ["VWAP", "ORB", "Momentum"], key="int_mode")
        g1, g2 = st.columns(2)
        qty3       = g1.number_input("Qty (shares/units)", 1, 50000, d["qty"], key="int_qty")
        entry_time3 = g2.text_input("Entry time HH:MM", d["entry_time"], key="int_et")
        h1, h2, h3 = st.columns(3)
        exit_time3 = h1.text_input("Exit time HH:MM", d["exit_time"], key="int_xt")
        tgt3       = h2.number_input("Target %", 0.1, 5.0, d["target_pct"], key="int_tp", step=0.1)
        sl3        = h3.number_input("SL %",     0.1, 3.0, d["sl_pct"],     key="int_sl", step=0.1)

        if mode3 == "ORB":
            i1, i2 = st.columns(2)
            orb_min    = i1.number_input("ORB window (min)", 5, 60, d["orb_minutes"], key="int_om")
            target_mult = i2.number_input("Target × range", 1.0, 5.0, 2.0, step=0.5, key="int_tm")
        elif mode3 == "Momentum":
            j1, j2 = st.columns(2)
            fast3 = j1.number_input("Fast EMA", 3, 50,  d["fast_ema"], key="int_fe")
            slow3 = j2.number_input("Slow EMA", 10, 200, d["slow_ema"], key="int_se")

        mode_str3 = "Paper 📄" if not live_mode else st.radio("Mode", ["Paper 📄", "Live 🔴"], horizontal=True, key="int_m")
        paper3    = "Paper" in mode_str3 or not live_mode

        params3 = {
            "symbol": sym3.upper(), "mode": mode3, "qty": qty3,
            "entry_time": entry_time3, "exit_time": exit_time3,
            "target_pct": tgt3, "sl_pct": sl3,
        }
        if mode3 == "ORB":
            params3["orb_minutes"]  = orb_min
            params3["target_mult"]  = target_mult
        elif mode3 == "Momentum":
            params3["fast_ema"] = fast3
            params3["slow_ema"] = slow3

        if st.button("🚀 Activate Intraday", type="primary", use_container_width=True, key="int_add"):
            sid = bot.add_run("Intraday", symbol=sym3.upper(), paper=paper3, params=params3)
            if not bot.is_running: bot.start()
            st.success(f"✅ Intraday **{mode3}** on **{sym3.upper()}** added (ID `{sid}`)")

    with right3:
        st.markdown("""
**Three intraday modes — all use MIS product type (auto square-off)**

| Mode | Signal |
|---|---|
| **VWAP** | Price 0.2% below VWAP + RSI < 45 → BUY ·  Price 0.2% above VWAP + RSI > 55 → SELL |
| **ORB** | After N-minute range builds: breakout above high → BUY · below low → SELL · SL at range midpoint |
| **Momentum** | EMA cross + RSI confirmation: golden cross + RSI > 55 → BUY · death cross + RSI < 45 → SELL |

**All modes exit when:**
- ✅ Price hits target % from entry
- 🛑 Price hits SL % against position
- ⏱ Auto square-off at exit time (latest 15:15)
        """)

# ══════════════════════════════════════════════════════════════════════════════
#  Tab 4 — Active Runs
# ══════════════════════════════════════════════════════════════════════════════
with tab_runs:
    st.subheader("📋 All Active Runs")

    bc1, bc2, bc3 = st.columns(3)
    if bc1.button("▶ Start" if not bot.is_running else "⏹ Stop",
                  type="primary" if not bot.is_running else "secondary",
                  use_container_width=True, key="sm_ss"):
        if bot.is_running: bot.stop()
        else:              bot.start()
        st.rerun()
    if bc2.button("🚨 Emergency Stop", use_container_width=True, key="sm_emg"):
        bot.emergency_stop(); st.rerun()
    if bc3.button("🗑 Clear finished", use_container_width=True, key="sm_clr"):
        bot.clear_done(); st.rerun()

    new_lim = st.number_input("Max daily loss ₹", value=float(abs(bot.max_daily_loss)),
                               min_value=500.0, step=500.0, key="sm_lim")
    bot.max_daily_loss = -abs(new_lim)

    st.divider()
    runs = bot.get_runs()
    if not runs:
        st.info("No strategy runs. Add one from the tabs above.")
    else:
        for run in runs:
            chip = {"WAITING": "🟡", "ACTIVE": "🟢", "EXITING": "🟠", "DONE": "✅", "ERROR": "❌"}.get(run.state, "⚪")
            pnl_c = "#065f46" if run.pnl >= 0 else "#dc2626"
            with st.container(border=True):
                c1, c2, c3, c4 = st.columns([4, 2, 2, 1])
                c1.markdown(f"**{run.name}** `{run.id}` · {run.symbol} · "
                            f"{'📄 Paper' if run.paper else '🔴 LIVE'} · _{run.created_at}_")
                c2.markdown(f"<span style='color:{pnl_c};font-weight:700'>₹{run.pnl:+,.0f}</span>",
                            unsafe_allow_html=True)
                c3.markdown(f"{chip} {run.state}")
                if c4.button("✕", key=f"rm_{run.id}"):
                    bot.remove_run(run.id); st.rerun()
                if run.log:
                    with st.expander("Log", expanded=False):
                        st.code("\n".join(f"[{e['ts']}] {e['msg']}"
                                         for e in reversed(run.log[-30:])), language="")
