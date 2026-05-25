"""
Strategy Manager — Options Chain · MTF · Intraday · Breakout · Bounce · Bulk Order · Screener
"""

import streamlit as st

from broker.groww import connector
from engine.bot import STRATEGY_DEFAULTS, bot
from engine.screener import NIFTY50, NIFTY_NEXT50, screener
from engine.risk_guard import risk_guard

st.set_page_config(page_title="Strategies", page_icon="⚙️", layout="wide")
st.title("⚙️ Strategy Manager")

live_mode = connector.is_connected

tab_oc, tab_mtf, tab_intra, tab_brk, tab_bnc, tab_bulk, tab_scr, tab_runs = st.tabs(
    ["📊 Options Chain", "💳 MTF", "⚡ Intraday",
     "🚀 Breakout", "📉 Bounce", "🔵 Bulk Order",
     "🔍 Screener", "📋 Active Runs"]
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
#  Tab 4 — Breakout
# ══════════════════════════════════════════════════════════════════════════════
with tab_brk:
    st.subheader("🚀 Breakout Strategy")
    st.caption(
        "Enters LONG when price breaks above the N-bar high on a volume surge, "
        "ideally after a Bollinger Band squeeze. CNC delivery product."
    )

    left4, right4 = st.columns([5, 5], gap="large")
    with left4:
        d4      = STRATEGY_DEFAULTS["Breakout"]
        sym4    = st.text_input("Stock symbol (NSE)", d4["symbol"].upper(), key="brk_sym")
        b4a, b4b = st.columns(2)
        qty4     = b4a.number_input("Qty (shares)", 1, 10000, d4["qty"],      key="brk_qty")
        lookb4   = b4b.number_input("Lookback bars (N)", 5, 60, d4["lookback"], key="brk_lb",
                                     help="Price must break above highest of last N daily bars")
        c4a, c4b, c4c = st.columns(3)
        vol4     = c4a.number_input("Min vol ratio", 1.0, 5.0, d4["vol_min"], step=0.1, key="brk_vr",
                                     help="Current volume must be ≥ this × 20-bar average")
        rr4      = c4b.number_input("Target R:R", 1.0, 5.0, d4["target_rr"], step=0.5, key="brk_rr",
                                     help="Target = entry + R:R × (entry − SL)")
        xt4      = c4c.text_input("Exit time HH:MM", d4["exit_time"], key="brk_xt")

        mode_str4 = "Paper 📄" if not live_mode else st.radio("Mode", ["Paper 📄", "Live 🔴"],
                                                               horizontal=True, key="brk_m")
        paper4    = "Paper" in mode_str4 or not live_mode

        # Quick scan
        if st.button("⚡ Quick scan for breakouts", key="brk_scan"):
            with st.spinner("Scanning Nifty 50 for breakout setups…"):
                res = screener.scan_breakout()
            st.success(f"Found {len(res)} breakout candidates")
            st.rerun()

        top_brk = screener.top_breakout(5)
        if top_brk:
            st.markdown("**🚀 Top Breakout Picks:**")
            for pick in top_brk:
                col_a, col_b = st.columns([3, 1])
                col_a.markdown(
                    f"**{pick['symbol']}** ₹{pick['price']:.2f} · "
                    f"vol×{pick['vol_ratio']:.1f} · RSI {pick.get('rsi', 0):.0f} · "
                    f"score {pick['score']}"
                )
                if col_b.button("Use", key=f"brk_use_{pick['symbol']}"):
                    st.session_state["brk_sym"] = pick["symbol"]
                    st.rerun()

        if st.button("🚀 Activate Breakout", type="primary", use_container_width=True, key="brk_add"):
            sid = bot.add_run("Breakout", symbol=sym4.upper(), paper=paper4, params={
                "symbol": sym4.upper(), "qty": qty4, "lookback": lookb4,
                "vol_min": vol4, "target_rr": rr4, "exit_time": xt4,
            })
            if not bot.is_running: bot.start()
            st.success(f"✅ Breakout on **{sym4.upper()}** added (ID `{sid}`)")

    with right4:
        st.markdown("""
**Breakout Logic**

The bot monitors daily price vs the highest close/high of the last N bars:

| Condition | Requirement |
|---|---|
| **Price** | Today's price > N-bar high × 1.001 |
| **Volume** | Current volume ≥ vol_ratio × 20-bar avg |
| **RSI** | RSI < 75 (not extremely overbought) |
| **Optional** | BB squeeze before breakout = stronger signal |

**Entry**: Market BUY at breakout price  
**SL**: Just below the N-bar high (0.5% buffer)  
**Target**: Entry + R:R × (Entry − SL)  
**Exit**: Target / SL / time exit

> 💡 Use the **⚡ Quick scan** button to find stocks currently in breakout setup.
> Best combined with VIX < 18 (trending market regime).
        """)

# ══════════════════════════════════════════════════════════════════════════════
#  Tab 5 — Bounce
# ══════════════════════════════════════════════════════════════════════════════
with tab_bnc:
    st.subheader("📉 Bounce Strategy (Mean Reversion)")
    st.caption(
        "Enters LONG when RSI is oversold and price touches the lower Bollinger Band "
        "with volume drying up. Target = BB midband. CNC delivery product."
    )

    left5, right5 = st.columns([5, 5], gap="large")
    with left5:
        d5      = STRATEGY_DEFAULTS["Bounce"]
        sym5    = st.text_input("Stock symbol (NSE)", d5["symbol"].upper(), key="bnc_sym")
        b5a, b5b = st.columns(2)
        qty5     = b5a.number_input("Qty (shares)", 1, 10000, d5["qty"],      key="bnc_qty")
        rsi5     = b5b.number_input("RSI threshold", 20, 55, d5["rsi_level"], key="bnc_rsi",
                                     help="Enter only when RSI is at or below this level")
        c5a, c5b, c5c = st.columns(3)
        atr5     = c5a.number_input("ATR SL mult", 1.0, 3.0, d5["atr_sl_mult"], step=0.25, key="bnc_atr",
                                     help="SL = entry − mult × ATR14")
        rr5      = c5b.number_input("Target R:R", 1.0, 5.0, d5["target_rr"], step=0.5, key="bnc_rr",
                                     help="Target defaults to BB midband; this is a fallback multiplier")
        xt5      = c5c.text_input("Exit time HH:MM", d5["exit_time"], key="bnc_xt")

        mode_str5 = "Paper 📄" if not live_mode else st.radio("Mode", ["Paper 📄", "Live 🔴"],
                                                               horizontal=True, key="bnc_m")
        paper5    = "Paper" in mode_str5 or not live_mode

        if st.button("⚡ Quick scan for bounces", key="bnc_scan"):
            with st.spinner("Scanning Nifty 50 for oversold bounce setups…"):
                res = screener.scan_bounce()
            st.success(f"Found {len(res)} bounce candidates")
            st.rerun()

        top_bnc = screener.top_bounce(5)
        if top_bnc:
            st.markdown("**📉 Top Bounce Picks:**")
            for pick in top_bnc:
                col_a, col_b = st.columns([3, 1])
                col_a.markdown(
                    f"**{pick['symbol']}** ₹{pick['price']:.2f} · "
                    f"RSI {pick.get('rsi', 0):.0f} · "
                    f"BB_low ₹{pick.get('bb_low', 0):.2f} · score {pick['score']}"
                )
                if col_b.button("Use", key=f"bnc_use_{pick['symbol']}"):
                    st.session_state["bnc_sym"] = pick["symbol"]
                    st.rerun()

        if st.button("📉 Activate Bounce", type="primary", use_container_width=True, key="bnc_add"):
            sid = bot.add_run("Bounce", symbol=sym5.upper(), paper=paper5, params={
                "symbol": sym5.upper(), "qty": qty5, "rsi_level": rsi5,
                "atr_sl_mult": atr5, "target_rr": rr5, "exit_time": xt5,
            })
            if not bot.is_running: bot.start()
            st.success(f"✅ Bounce on **{sym5.upper()}** added (ID `{sid}`)")

    with right5:
        st.markdown("""
**Bounce (Mean Reversion) Logic**

Looks for stocks that are deeply oversold and near the lower Bollinger Band:

| Condition | Requirement |
|---|---|
| **RSI** | RSI ≤ threshold (default 40) |
| **BB Position** | Price ≤ lower BB × 1.02 |
| **Volume** | Low vol_ratio ≤ 0.85 (sellers exhausted — optional boost) |
| **RSI curl** | RSI just turned up from lower reading (early reversal boost) |

**Entry**: Market BUY  
**SL**: Entry − ATR_mult × ATR14  
**Target**: BB Midband (20-SMA)  
**Exit**: Target / SL / time exit

> ⚠️ Best in VOLATILE or NORMAL regime (VIX > 14).
> Avoid in strong downtrends — use ADX < 25 as additional filter.
        """)

# ══════════════════════════════════════════════════════════════════════════════
#  Tab 6 — Bulk Order
# ══════════════════════════════════════════════════════════════════════════════
with tab_bulk:
    st.subheader("🔵 Bulk Order (Institutional Volume Spike)")
    st.caption(
        "Detects a 5-minute bar with unusually high volume (institutional bulk order). "
        "Enters in the direction of the price move. Fast intraday MIS trade."
    )

    left6, right6 = st.columns([5, 5], gap="large")
    with left6:
        d6       = STRATEGY_DEFAULTS["Bulk Order"]
        sym6     = st.text_input("Stock symbol (NSE)", d6["symbol"].upper(), key="blk_sym")
        b6a, b6b = st.columns(2)
        qty6     = b6a.number_input("Qty (shares)", 1, 50000, d6["qty"],         key="blk_qty")
        bulk6    = b6b.number_input("Min vol spike ×", 1.5, 10.0, d6["bulk_ratio"], step=0.5,
                                     key="blk_bk",
                                     help="Current 5-min volume must be ≥ this × last 20-bar avg")
        c6a, c6b, c6c = st.columns(3)
        tgt6     = c6a.number_input("Target %", 0.1, 3.0, d6["target_pct"], step=0.1, key="blk_tp")
        sl6      = c6b.number_input("SL %",     0.1, 2.0, d6["sl_pct"],     step=0.1, key="blk_sl")
        xt6      = c6c.text_input("Exit time HH:MM", d6["exit_time"], key="blk_xt")

        mode_str6 = "Paper 📄" if not live_mode else st.radio("Mode", ["Paper 📄", "Live 🔴"],
                                                               horizontal=True, key="blk_m")
        paper6    = "Paper" in mode_str6 or not live_mode

        if st.button("⚡ Scan for bulk spikes", key="blk_scan"):
            with st.spinner("Scanning 5-min data for institutional volume spikes…"):
                res = screener.scan_bulk()
            st.success(f"Found {len(res)} bulk order candidates")
            st.rerun()

        top_blk = screener.top_bulk(5)
        if top_blk:
            st.markdown("**🔵 Top Bulk Order Picks:**")
            for pick in top_blk:
                col_a, col_b = st.columns([3, 1])
                col_a.markdown(
                    f"**{pick['symbol']}** ₹{pick['price']:.2f} · "
                    f"vol×{pick.get('bulk_ratio', 0):.1f} · "
                    f"{pick.get('direction','—')} · score {pick['score']}"
                )
                if col_b.button("Use", key=f"blk_use_{pick['symbol']}"):
                    st.session_state["blk_sym"] = pick["symbol"]
                    st.rerun()

        if st.button("🔵 Activate Bulk Order", type="primary", use_container_width=True, key="blk_add"):
            sid = bot.add_run("Bulk Order", symbol=sym6.upper(), paper=paper6, params={
                "symbol": sym6.upper(), "qty": qty6, "bulk_ratio": bulk6,
                "target_pct": tgt6, "sl_pct": sl6, "exit_time": xt6,
            })
            if not bot.is_running: bot.start()
            st.success(f"✅ Bulk Order on **{sym6.upper()}** added (ID `{sid}`)")

    with right6:
        st.markdown("""
**Bulk Order (Institutional Activity) Logic**

Monitors 5-minute bars for sudden volume spikes — a footprint of institutional buying or selling:

| Condition | Requirement |
|---|---|
| **5-min Volume** | Current bar ≥ bulk_ratio × 20-bar 5-min avg |
| **Direction** | BUY if price moved up · SELL if price moved down on the spike |
| **Daily RSI** | 40–70 (LONG) or 30–60 (SHORT) for quality filter |

**Entry**: Market order immediately on spike detection  
**SL**: SL% from entry (tight — this is a quick intraday play)  
**Target**: Target% from entry  
**Product**: MIS (intraday auto square-off)  
**Exit**: Target / SL / time exit

> 💡 Lower bulk_ratio threshold (2.5×) catches more signals.
> Higher threshold (4×+) gives only strong institutional moves.
        """)

# ══════════════════════════════════════════════════════════════════════════════
#  Tab 7 — Smart Screener
# ══════════════════════════════════════════════════════════════════════════════
with tab_scr:
    st.subheader("🔍 Smart Stock Screener")
    st.caption(
        "Multi-factor scoring engine. Scans Nifty 50/100/F&O universe using "
        "RSI · EMA alignment · BB position · Volume surge · ADX · MACD. "
        "Detects market regime via VIX and ranks stocks for MTF or Intraday."
    )

    # ── Configuration ─────────────────────────────────────────────────────────
    sc1, sc2, sc3 = st.columns(3)
    with sc1:
        uni = st.selectbox("Universe", ["nifty50", "nifty100", "fno", "custom"],
                           key="scr_uni",
                           help="nifty50=50 stocks · nifty100=100 · fno=150+ · custom=your list")
    with sc2:
        top_n = st.slider("Show top N per category", 3, 15, 5, key="scr_n")
    with sc3:
        custom_raw = st.text_input("Custom symbols (comma-separated)", "",
                                   key="scr_custom",
                                   help="Only used when universe=custom. E.g. RELIANCE,INFY,TCS")

    # Capital & risk settings
    with st.expander("⚙️ Risk Guard settings", expanded=False):
        rg1, rg2, rg3, rg4 = st.columns(4)
        risk_guard.capital            = rg1.number_input("Capital ₹", 10_000.0, 50_000_000.0,
                                                          risk_guard.capital, step=10_000.0, key="rg_cap")
        risk_guard.risk_per_trade_pct = rg2.number_input("Risk per trade %", 0.25, 5.0,
                                                           risk_guard.risk_per_trade_pct,
                                                           step=0.25, key="rg_rpt")
        risk_guard.max_open_trades    = rg3.number_input("Max open trades", 1, 20,
                                                          risk_guard.max_open_trades, key="rg_mot")
        risk_guard.atr_sl_mult        = rg4.number_input("ATR SL mult", 1.0, 5.0,
                                                          risk_guard.atr_sl_mult,
                                                          step=0.5, key="rg_asl")

    # Auto-screener bot settings
    with st.expander("🤖 Auto-screener bot", expanded=False):
        ab1, ab2, ab3 = st.columns(3)
        bot.auto_screener  = ab1.toggle("Enable auto-screener", bot.auto_screener, key="scr_auto")
        bot.auto_max_runs  = ab2.number_input("Max auto runs", 1, 10, bot.auto_max_runs, key="scr_mr")
        bot.auto_paper     = ab3.toggle("Auto runs in paper mode", bot.auto_paper, key="scr_ppr")
        if bot.auto_screener:
            st.info("🤖 Auto-screener ON — bot will pick top stocks from each scan and create "
                    "MTF/Intraday runs automatically every 15 minutes.")

    # ── Scan ──────────────────────────────────────────────────────────────────
    sa1, sa2 = st.columns([3, 1])
    with sa1:
        last_ts = screener.last_scan.strftime("%H:%M:%S") if screener.last_scan else "Never"
        st.caption(f"Last scan: **{last_ts}** · Regime: **{screener.regime_label()}**")
    with sa2:
        run_scan = st.button("🔄 Run scan now", type="primary",
                             use_container_width=True, key="scr_scan")

    if run_scan:
        screener.universe       = uni
        screener.custom_symbols = [s.strip().upper() for s in custom_raw.split(",") if s.strip()]
        with st.spinner("Scanning universe… this takes ~30 seconds for 50+ stocks"):
            results = screener.scan()
        st.success(f"✅ Scanned {len(results)} stocks · Regime: {screener.regime_label()}")
        st.rerun()

    # ── Results display ───────────────────────────────────────────────────────
    results = screener.get_results()
    if not results:
        st.info("Click **Run scan now** to analyse the stock universe.")
    else:
        import pandas as pd
        from data import feed as _feed

        vix = _feed.spot("VIX") or 15

        # ── Regime banner ──────────────────────────────────────────────────────
        regime = screener.regime
        if   regime == "TRENDING":
            st.success(f"📈 Market Regime: **TRENDING** (VIX={vix:.1f}) — Momentum strategies favoured")
        elif regime == "VOLATILE":
            st.warning(f"⚡ Market Regime: **VOLATILE** (VIX={vix:.1f}) — Mean reversion favoured")
        else:
            st.info(f"↔️ Market Regime: **NORMAL** (VIX={vix:.1f}) — Both strategies work")

        # ── Live price refresh for screened stocks ─────────────────────────────
        if st.button("⚡ Refresh live prices", key="scr_lp_refresh"):
            syms = list(results.keys())[:30]
            with st.spinner("Fetching live prices…"):
                live = _feed.batch_refresh(syms)
            # Merge live prices into results
            for sym, q in live.items():
                if sym in results:
                    results[sym]["live_price"]  = q.get("price", results[sym]["price"])
                    results[sym]["live_chg_pct"] = q.get("change_pct", 0.0)
            st.success(f"✅ Updated {len(live)} prices")

        # ── Top momentum ───────────────────────────────────────────────────────
        st.markdown("### 🚀 Top Momentum Picks (Intraday)")
        mom_picks = screener.top_momentum(top_n)
        if mom_picks:
            # Table with live price column
            rows = []
            for s in mom_picks:
                lp   = s.get("live_price",   s["price"])
                chg  = s.get("live_chg_pct", 0.0)
                rows.append({
                    "Symbol":    s["symbol"],
                    "Live ₹":   f"₹{lp:,.2f}",
                    "Chg %":    f"{'▲' if chg>=0 else '▼'} {chg:+.2f}%",
                    "RSI":       round(s["rsi"], 0),
                    "ADX":       round(s["adx"], 0) if s.get("adx") else "—",
                    "Vol Ratio": round(s.get("vol_ratio") or 1.0, 2),
                    "ATR %":     s["atr_pct"],
                    "⭐ Score":  s["mom_score"],
                })
            st.dataframe(pd.DataFrame(rows), use_container_width=True, hide_index=True,
                         column_config={
                             "⭐ Score": st.column_config.ProgressColumn("⭐ Score", min_value=0, max_value=100),
                         })
            # Action buttons: Chart preview + Quick add Intraday
            qc = st.columns(min(top_n, len(mom_picks)) * 2)
            for i, row in enumerate(mom_picks[:top_n]):
                sym   = row["symbol"]
                sp    = row.get("live_price", row["price"])
                atr_v = row["atr_pct"] / 100 * sp
                sl_p  = risk_guard.sl_pct(sp, atr_v, vix)
                tgt_p = risk_guard.target_pct(sp, atr_v, vix)
                qty   = risk_guard.position_size(sp, atr_v, vix,
                                                  sum(1 for r in bot.get_runs()
                                                      if r.state in ("WAITING","ACTIVE")))
                # Chart button
                if qc[i*2].button(f"📈 {sym}", key=f"chart_mom_{sym}",
                                   help=f"Open live chart for {sym}"):
                    st.session_state["chart_symbol"] = sym
                    st.switch_page("pages/4_📈_Charts.py")
                # Quick add button
                if qc[i*2+1].button(f"⚡ Trade", key=f"qadd_intr_{sym}",
                                     help=f"Add Intraday · Score {row['mom_score']} · "
                                          f"SL {sl_p:.1f}% · Tgt {tgt_p:.1f}% · Qty {qty}"):
                    sid = bot.add_run("Intraday", symbol=sym, paper=not live_mode, params={
                        "symbol": sym, "mode": "Momentum", "qty": qty,
                        "entry_time": "09:20", "exit_time": "15:10",
                        "target_pct": tgt_p, "sl_pct": sl_p,
                        "fast_ema": 9, "slow_ema": 21, "use_atr_risk": True,
                    })
                    if not bot.is_running: bot.start()
                    st.success(f"✅ Added Intraday `{sid}` for {sym}")
                    st.rerun()

        # ── Top reversion ──────────────────────────────────────────────────────
        st.markdown("### 📉 Top Mean Reversion Picks (MTF)")
        rev_picks = screener.top_reversion(top_n)
        if rev_picks:
            rows2 = []
            for s in rev_picks:
                lp  = s.get("live_price",   s["price"])
                chg = s.get("live_chg_pct", 0.0)
                rows2.append({
                    "Symbol":   s["symbol"],
                    "Live ₹":  f"₹{lp:,.2f}",
                    "Chg %":   f"{'▲' if chg>=0 else '▼'} {chg:+.2f}%",
                    "RSI":      round(s["rsi"], 0),
                    "BB Width": f"{s.get('bb_width', 0):.1f}%",
                    "ATR %":    s["atr_pct"],
                    "% 52H":    f"{s.get('pct_from_52h', 0):+.1f}%",
                    "⭐ Score": s["rev_score"],
                })
            st.dataframe(pd.DataFrame(rows2), use_container_width=True, hide_index=True,
                         column_config={
                             "⭐ Score": st.column_config.ProgressColumn("⭐ Score", min_value=0, max_value=100),
                         })
            qc2 = st.columns(min(top_n, len(rev_picks)) * 2)
            for i, row in enumerate(rev_picks[:top_n]):
                sym   = row["symbol"]
                sp    = row.get("live_price", row["price"])
                atr_v = row["atr_pct"] / 100 * sp
                sl_p  = risk_guard.sl_pct(sp, atr_v, vix)
                tgt_p = risk_guard.target_pct(sp, atr_v, vix)
                qty   = risk_guard.position_size(sp, atr_v, vix,
                                                  sum(1 for r in bot.get_runs()
                                                      if r.state in ("WAITING","ACTIVE")))
                if qc2[i*2].button(f"📈 {sym}", key=f"chart_rev_{sym}",
                                    help=f"Open live chart for {sym}"):
                    st.session_state["chart_symbol"] = sym
                    st.switch_page("pages/4_📈_Charts.py")
                if qc2[i*2+1].button(f"💳 Trade", key=f"qadd_mtf_{sym}",
                                      help=f"Add MTF · Score {row['rev_score']} · "
                                           f"SL {sl_p:.1f}% · Tgt {tgt_p:.1f}% · Qty {qty}"):
                    sid = bot.add_run("MTF", symbol=sym, paper=not live_mode, params={
                        "symbol": sym, "signal": "RSI Bounce", "fast_ema": 9,
                        "slow_ema": 21, "rsi_level": 35, "qty": qty,
                        "target_pct": tgt_p, "sl_pct": sl_p, "max_days": 3,
                        "use_atr_risk": True,
                    })
                    if not bot.is_running: bot.start()
                    st.success(f"✅ Added MTF `{sid}` for {sym}")
                    st.rerun()

        # ── Top Breakout picks ─────────────────────────────────────────────────
        st.markdown("### 🚀 Breakout Picks")
        scr_brk = screener.top_breakout(top_n)
        if not scr_brk:
            sc_b1, sc_b2 = st.columns([3, 1])
            sc_b1.caption("No breakout data. Run a breakout scan.")
            if sc_b2.button("⚡ Scan Breakouts", key="scr_brk_scan"):
                with st.spinner("Scanning…"):
                    screener.scan_breakout()
                st.rerun()
        else:
            brk_rows = []
            for s in scr_brk:
                brk_rows.append({
                    "Symbol":     s["symbol"],
                    "Price ₹":    f"₹{s['price']:,.2f}",
                    "Vol Ratio":  round(s.get("vol_ratio") or 0, 2),
                    "RSI":        round(s.get("rsi") or 0, 0),
                    "H20 ₹":      f"₹{s.get('high20', 0):,.2f}",
                    "SL ₹":       f"₹{s.get('sl', 0):,.2f}",
                    "Tgt ₹":      f"₹{s.get('target', 0):,.2f}",
                    "BB Squeeze": "✓" if s.get("was_squeezed") else "—",
                    "⭐ Score":   s["score"],
                })
            st.dataframe(pd.DataFrame(brk_rows), use_container_width=True, hide_index=True,
                         column_config={"⭐ Score": st.column_config.ProgressColumn("⭐ Score", min_value=0, max_value=100)})
            qcb = st.columns(min(top_n, len(scr_brk)) * 2)
            for i, row in enumerate(scr_brk[:top_n]):
                sym   = row["symbol"]
                sp    = row["price"]
                atr_v = row["atr_pct"] / 100 * sp
                qty   = risk_guard.position_size(sp, atr_v, vix,
                                                  sum(1 for r in bot.get_runs()
                                                      if r.state in ("WAITING","ACTIVE")))
                if qcb[i*2].button(f"📈 {sym}", key=f"chart_brk_{sym}"):
                    st.session_state["chart_symbol"] = sym
                    st.switch_page("pages/4_📈_Charts.py")
                if qcb[i*2+1].button(f"🚀 Trade", key=f"qadd_brk_{sym}",
                                      help=f"Add Breakout · Score {row['score']}"):
                    sid = bot.add_run("Breakout", symbol=sym, paper=not live_mode, params={
                        "symbol": sym, "qty": qty, "lookback": 20,
                        "vol_min": 1.5, "target_rr": 2.0, "exit_time": "15:10",
                    })
                    if not bot.is_running: bot.start()
                    st.success(f"✅ Added Breakout `{sid}` for {sym}")
                    st.rerun()

        # ── Top Bounce picks ───────────────────────────────────────────────────
        st.markdown("### 📉 Bounce Picks (Mean Reversion)")
        scr_bnc = screener.top_bounce(top_n)
        if not scr_bnc:
            sc_c1, sc_c2 = st.columns([3, 1])
            sc_c1.caption("No bounce data. Run a bounce scan.")
            if sc_c2.button("⚡ Scan Bounces", key="scr_bnc_scan"):
                with st.spinner("Scanning…"):
                    screener.scan_bounce()
                st.rerun()
        else:
            bnc_rows = []
            for s in scr_bnc:
                bnc_rows.append({
                    "Symbol":   s["symbol"],
                    "Price ₹":  f"₹{s['price']:,.2f}",
                    "RSI":      round(s.get("rsi") or 0, 0),
                    "BB Low ₹": f"₹{s.get('bb_low', 0):,.2f}",
                    "BB Mid ₹": f"₹{s.get('bb_mid', 0):,.2f}",
                    "SL ₹":     f"₹{s.get('sl', 0):,.2f}",
                    "Tgt ₹":    f"₹{s.get('target', 0):,.2f}",
                    "% 52H":    f"{s.get('pct_from_52h', 0):+.1f}%",
                    "⭐ Score": s["score"],
                })
            st.dataframe(pd.DataFrame(bnc_rows), use_container_width=True, hide_index=True,
                         column_config={"⭐ Score": st.column_config.ProgressColumn("⭐ Score", min_value=0, max_value=100)})
            qcc = st.columns(min(top_n, len(scr_bnc)) * 2)
            for i, row in enumerate(scr_bnc[:top_n]):
                sym   = row["symbol"]
                sp    = row["price"]
                atr_v = row["atr_pct"] / 100 * sp
                qty   = risk_guard.position_size(sp, atr_v, vix,
                                                  sum(1 for r in bot.get_runs()
                                                      if r.state in ("WAITING","ACTIVE")))
                if qcc[i*2].button(f"📈 {sym}", key=f"chart_bnc_{sym}"):
                    st.session_state["chart_symbol"] = sym
                    st.switch_page("pages/4_📈_Charts.py")
                if qcc[i*2+1].button(f"📉 Trade", key=f"qadd_bnc_{sym}",
                                      help=f"Add Bounce · Score {row['score']}"):
                    sid = bot.add_run("Bounce", symbol=sym, paper=not live_mode, params={
                        "symbol": sym, "qty": qty, "rsi_level": 40,
                        "atr_sl_mult": 1.5, "target_rr": 2.0, "exit_time": "15:10",
                    })
                    if not bot.is_running: bot.start()
                    st.success(f"✅ Added Bounce `{sid}` for {sym}")
                    st.rerun()

        # ── Full results table ─────────────────────────────────────────────────
        with st.expander(f"📊 Full scan results ({len(results)} stocks)", expanded=False):
            all_df = pd.DataFrame(results.values())[
                ["symbol", "price", "rsi", "adx", "vol_ratio", "atr_pct",
                 "mom_score", "rev_score", "composite", "signal", "fit_mtf", "fit_intraday"]
            ].sort_values("composite", ascending=False)
            # Chart button per row
            st.dataframe(all_df, use_container_width=True, hide_index=True)
            st.caption("Click 📈 in Momentum / Reversion sections above to open live chart for any stock.")


# ══════════════════════════════════════════════════════════════════════════════
#  Tab 8 — Active Runs
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
