"""
Strategy Manager — Add, configure, and remove strategy runs.
"""

import streamlit as st

from broker.groww import connector
from engine.bot import STRATEGY_DEFAULTS, STRATEGY_NAMES, LOT_SIZES, bot

st.set_page_config(page_title="Strategies", page_icon="⚙️", layout="wide")
st.title("⚙️ Strategy Manager")

live_mode = connector.is_connected

left, right = st.columns([4, 6], gap="large")

# ══════════════════════════════════════
#  LEFT — Add new strategy run
# ══════════════════════════════════════
with left:
    st.subheader("➕ New Strategy Run")

    name   = st.selectbox("Strategy", STRATEGY_NAMES, key="sm_name")
    symbol = st.selectbox(
        "Symbol / Index",
        ["NIFTY", "BANKNIFTY", "FINNIFTY", "MIDCAPNIFTY", "SBIN", "RELIANCE", "INFY", "TCS"],
        key="sm_sym",
    )

    _l1, _l2 = st.columns(2)
    lot_sz = LOT_SIZES.get(symbol, 1)
    lots   = _l1.number_input(f"Lots (1 = {lot_sz} qty)", 1, 200, 1, key="sm_lots")
    mode   = _l2.selectbox("Mode", ["Paper 📄", "Live 🔴" if live_mode else "Live (need auth)"],
                           key="sm_mode")
    paper  = "Paper" in mode or not live_mode

    if not live_mode and "Live" in mode:
        st.warning("Groww not connected — falling back to Paper mode.")

    st.caption("**Parameters**")
    defs   = STRATEGY_DEFAULTS.get(name, {})
    params: dict = {}

    if name in ("Short Straddle", "Iron Condor"):
        _a, _b = st.columns(2)
        params["entry_time"] = _a.text_input("Entry time HH:MM",
                                              defs.get("entry_time", "09:20"), key="sm_et")
        params["exit_time"]  = _b.text_input("Exit time  HH:MM",
                                              defs.get("exit_time",  "15:15"), key="sm_xt")
        _c, _d = st.columns(2)
        params["target_pct"] = _c.number_input("Target % of premium",
                                                10, 100, defs.get("target_pct", 50), key="sm_tp")
        if name == "Short Straddle":
            params["sl_pct"] = _d.number_input("SL % of premium",
                                                50, 300, defs.get("sl_pct", 100), key="sm_sl")
        else:
            params["wing_width"] = _d.number_input("Wing width (strikes)",
                                                    1, 6, defs.get("wing_width", 2), key="sm_ww")

    elif name == "ORB Breakout":
        _a, _b = st.columns(2)
        params["orb_minutes"]    = _a.number_input("ORB window (min)", 5, 60,
                                                    defs.get("orb_minutes", 15), key="sm_om")
        params["exit_time"]      = _b.text_input("Exit time HH:MM",
                                                  defs.get("exit_time", "15:15"), key="sm_xt2")
        _c, _d = st.columns(2)
        params["sl_buffer_pct"]  = _c.number_input("SL buffer %", 0.1, 2.0,
                                                    defs.get("sl_buffer_pct", 0.3),
                                                    step=0.1, key="sm_slb")
        params["target_mult"]    = _d.number_input("Target × range", 1.0, 5.0,
                                                    defs.get("target_mult", 2.0),
                                                    step=0.5, key="sm_tm")

    elif name == "EMA Crossover":
        _a, _b = st.columns(2)
        params["fast_ema"]  = _a.number_input("Fast EMA", 3,  50, defs.get("fast_ema", 9),  key="sm_fe")
        params["slow_ema"]  = _b.number_input("Slow EMA", 10, 200, defs.get("slow_ema", 21), key="sm_se")
        params["exit_time"] = st.text_input("Exit time HH:MM",
                                             defs.get("exit_time", "15:15"), key="sm_xt3")

    # Strategy description
    descriptions = {
        "Short Straddle":
            "Sell ATM CE + PE simultaneously. Collect the combined premium as income. "
            "Profit if market stays near ATM. Exit when premium decays by target%, "
            "or stop out if premium doubles.",
        "Iron Condor":
            "Sell ATM±wing CE and PE (inner legs), buy further OTM for protection (outer legs). "
            "Defined-risk premium collection strategy. Profits if market stays in a range.",
        "ORB Breakout":
            "Wait for the first N minutes to define the opening range (high/low). "
            "Enter long on upside breakout, short on downside breakout. "
            "SL at range midpoint, target at 2× range width.",
        "EMA Crossover":
            "Enter BUY when fast EMA crosses above slow EMA (golden cross). "
            "Enter SELL when fast EMA crosses below slow EMA (death cross). "
            "Exit on reversal or at end-of-day time.",
    }
    st.info(f"ℹ️ {descriptions.get(name, '')}")

    if st.button("🚀 Activate Strategy", type="primary", use_container_width=True, key="sm_add"):
        sid = bot.add_run(name=name, symbol=symbol, lots=lots, paper=paper, params=params)
        if not bot.is_running:
            bot.start()
        st.success(f"✅ **{name}** added (ID: `{sid}`) — bot started.")
        st.rerun()

# ══════════════════════════════════════
#  RIGHT — Current runs
# ══════════════════════════════════════
with right:
    st.subheader("📋 Active Runs")

    runs = bot.get_runs()
    if not runs:
        st.info("No strategy runs yet. Add one on the left.")
    else:
        for run in runs:
            chip_cls = {
                "WAITING": ("🟡", "#92400e", "#fef3c7"),
                "ACTIVE":  ("🟢", "#065f46", "#d1fae5"),
                "EXITING": ("🟠", "#9d174d", "#fce7f3"),
                "DONE":    ("✅", "#5b21b6", "#ede9fe"),
                "ERROR":   ("❌", "#991b1b", "#fee2e2"),
            }.get(run.state, ("⚪", "#374151", "#f3f4f6"))

            icon, txt_c, bg_c = chip_cls
            pnl_c = "#065f46" if run.pnl >= 0 else "#dc2626"

            with st.container(border=True):
                rc1, rc2, rc3, rc4 = st.columns([3, 1.5, 1.5, 1])
                rc1.markdown(
                    f"<b>{run.name}</b> <code>{run.id}</code><br>"
                    f"<span style='font-size:.8rem;color:#6b7280'>"
                    f"{run.symbol} · {run.lots} lot{'s' if run.lots>1 else ''} · "
                    f"{'📄 Paper' if run.paper else '🔴 LIVE'} · added {run.created_at}"
                    f"</span>",
                    unsafe_allow_html=True,
                )
                rc2.markdown(
                    f"<div style='color:#6b7280;font-size:.72rem'>P&L</div>"
                    f"<b style='color:{pnl_c}'>₹{run.pnl:+,.0f}</b>",
                    unsafe_allow_html=True,
                )
                rc3.markdown(
                    f"<div style='color:#6b7280;font-size:.72rem'>State</div>"
                    f"<span style='background:{bg_c};color:{txt_c};border-radius:4px;"
                    f"padding:2px 8px;font-size:.76rem;font-weight:700'>{icon} {run.state}</span>",
                    unsafe_allow_html=True,
                )
                if rc4.button("Remove", key=f"rm_{run.id}", use_container_width=True):
                    bot.remove_run(run.id); st.rerun()

                # Key params summary
                p = run.params
                summary_parts = []
                if "entry_time" in p: summary_parts.append(f"entry {p['entry_time']}")
                if "exit_time"  in p: summary_parts.append(f"exit {p['exit_time']}")
                if "target_pct" in p: summary_parts.append(f"tgt {p['target_pct']}%")
                if "sl_pct"     in p: summary_parts.append(f"SL {p['sl_pct']}%")
                if "fast_ema"   in p: summary_parts.append(f"EMA {p['fast_ema']}/{p['slow_ema']}")
                if "orb_minutes" in p: summary_parts.append(f"ORB {p['orb_minutes']}min")
                if summary_parts:
                    st.caption(" · ".join(summary_parts))

    st.divider()

    # ── Bot quick controls ────────────────────────────────────────────────────
    st.subheader("⚙️ Bot Controls")
    _bc1, _bc2 = st.columns(2)
    if _bc1.button("▶ Start" if not bot.is_running else "⏹ Stop",
                   type="primary" if not bot.is_running else "secondary",
                   use_container_width=True, key="sm_startstop"):
        if bot.is_running: bot.stop()
        else:              bot.start()
        st.rerun()
    if _bc2.button("🚨 Emergency Stop", use_container_width=True, key="sm_emg"):
        bot.emergency_stop(); st.rerun()

    new_lim = st.number_input(
        "Max daily loss ₹",
        value=float(abs(bot.max_daily_loss)), min_value=500.0, step=500.0, key="sm_lim"
    )
    bot.max_daily_loss = -abs(new_lim)

    if st.button("🗑 Clear finished runs", use_container_width=True, key="sm_clear"):
        bot.clear_done(); st.rerun()
