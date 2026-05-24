"""
Groww AutoTrader — Live Dashboard
==================================
Monitors running strategies in real-time. Auto-refreshes every 5s while bot is active.
"""

import time
from datetime import datetime

import pandas as pd
import plotly.graph_objects as go
import streamlit as st

from broker.groww import connector
from data import db, feed
from engine.bot import STRATEGY_NAMES, bot

st.set_page_config(page_title="AutoTrader", page_icon="🤖", layout="wide")

# ── CSS ────────────────────────────────────────────────────────────────────────
st.markdown("""
<style>
.chip { border-radius:4px; padding:2px 10px; font-size:.76rem; font-weight:700; }
.chip-wait  { background:#fef3c7; color:#92400e; }
.chip-on    { background:#d1fae5; color:#065f46; }
.chip-done  { background:#ede9fe; color:#5b21b6; }
.chip-err   { background:#fee2e2; color:#991b1b; }
.chip-exit  { background:#fce7f3; color:#9d174d; }
.kpi { background:#f9fafb; border:1px solid #e5e7eb; border-radius:10px;
       padding:14px 18px; text-align:center; }
.kpi-num  { font-size:1.4rem; font-weight:800; }
.kpi-lbl  { font-size:.72rem; color:#6b7280; margin-bottom:2px; }
</style>
""", unsafe_allow_html=True)

# ── Sidebar ────────────────────────────────────────────────────────────────────
with st.sidebar:
    st.image("https://img.icons8.com/fluency/48/robot-2.png", width=48)
    st.title("AutoTrader")
    st.caption("Automated strategy execution via Groww API")

    st.divider()
    if connector.is_connected:
        st.success("🟢 Groww connected — LIVE orders enabled")
        m = connector.margin()
        st.metric("Available margin", f"₹{m.get('available', 0):,.0f}")
        st.metric("F&O margin",       f"₹{m.get('fno', 0):,.0f}")
    else:
        st.info("🔵 Not connected — Paper mode only")
        st.page_link("pages/3_🔑_Broker_Connect.py", label="Connect Groww →")

    st.divider()
    st.caption("Pages")
    st.page_link("app.py",                             label="📊 Dashboard")
    st.page_link("pages/1_⚙️_Strategies.py",          label="⚙️ Strategy Manager")
    st.page_link("pages/2_📋_History.py",              label="📋 Trade History")
    st.page_link("pages/3_🔑_Broker_Connect.py",       label="🔑 Broker Connect")

# ── Header ─────────────────────────────────────────────────────────────────────
st.title("📊 Live Dashboard")

runs       = bot.get_runs()
active_cnt = sum(1 for r in runs if r.state == "ACTIVE")
total_pnl  = sum(r.pnl for r in runs)
d_pnl      = db.daily_pnl()
loss_pct   = abs(bot.daily_pnl / bot.max_daily_loss * 100) if bot.max_daily_loss else 0

# ── KPI bar ────────────────────────────────────────────────────────────────────
k1, k2, k3, k4, k5 = st.columns(5)
k1.markdown(
    f"<div class='kpi'><div class='kpi-lbl'>Bot</div>"
    f"<div class='kpi-num' style='color:{'#065f46' if bot.is_running else '#6b7280'}'>"
    f"{'▶ ON' if bot.is_running else '⏹ OFF'}</div></div>", unsafe_allow_html=True)
k2.markdown(
    f"<div class='kpi'><div class='kpi-lbl'>Active runs</div>"
    f"<div class='kpi-num'>{active_cnt}</div></div>", unsafe_allow_html=True)
k3.markdown(
    f"<div class='kpi'><div class='kpi-lbl'>Session P&L</div>"
    f"<div class='kpi-num' style='color:{'#065f46' if total_pnl>=0 else '#dc2626'}'>"
    f"₹{total_pnl:+,.0f}</div></div>", unsafe_allow_html=True)
k4.markdown(
    f"<div class='kpi'><div class='kpi-lbl'>Daily P&L</div>"
    f"<div class='kpi-num' style='color:{'#065f46' if d_pnl>=0 else '#dc2626'}'>"
    f"₹{d_pnl:+,.0f}</div></div>", unsafe_allow_html=True)
k5.markdown(
    f"<div class='kpi'><div class='kpi-lbl'>Loss limit used</div>"
    f"<div class='kpi-num' style='color:{'#dc2626' if loss_pct>75 else '#374151'}'>"
    f"{loss_pct:.0f}%</div></div>", unsafe_allow_html=True)

st.write("")

# ── Bot controls ───────────────────────────────────────────────────────────────
bc1, bc2, bc3, bc4 = st.columns([2, 2, 2, 4])
if bc1.button("▶ Start" if not bot.is_running else "⏹ Stop",
              type="primary" if not bot.is_running else "secondary",
              use_container_width=True, key="dash_start"):
    if bot.is_running:
        bot.stop()
    else:
        bot.start()
    st.rerun()

if bc2.button("🚨 Emergency Stop", use_container_width=True, key="dash_emg",
              help="Square off ALL positions now"):
    bot.emergency_stop()
    st.rerun()

if bc3.button("🗑 Clear finished", use_container_width=True, key="dash_clear"):
    bot.clear_done()
    st.rerun()

new_limit = bc4.number_input(
    "Max daily loss ₹", value=float(abs(bot.max_daily_loss)),
    min_value=500.0, step=500.0, label_visibility="collapsed",
    help="Kill-switch: bot stops all trades when daily loss hits this", key="dash_limit"
)
bot.max_daily_loss = -abs(new_limit)

st.divider()

# ── Market snapshot ────────────────────────────────────────────────────────────
st.subheader("📡 Market Snapshot")
_syms = ["NIFTY", "BANKNIFTY", "FINNIFTY", "VIX"]
_cols = st.columns(4)
for col, sym in zip(_cols, _syms):
    q   = feed.get_price(sym) or {}
    px  = q.get("price", "—")
    chg = q.get("change_pct", 0)
    clr = "#065f46" if chg >= 0 else "#dc2626"
    col.markdown(
        f"<div class='kpi'><div class='kpi-lbl'>{sym}</div>"
        f"<div style='font-weight:700;font-size:1.1rem'>₹{px:,.2f}" if isinstance(px, float)
        else f"<div style='font-weight:700;font-size:1.1rem'>—"
        f"</div><div style='color:{clr};font-size:.82rem;font-weight:600'>{chg:+.2f}%</div></div>",
        unsafe_allow_html=True,
    )

st.divider()

# ── Strategy run cards ─────────────────────────────────────────────────────────
st.subheader("🏃 Running Strategies")

if not runs:
    st.markdown("""
    <div style="background:#f9fafb;border:1px solid #e5e7eb;border-radius:12px;
    padding:48px;text-align:center">
    <div style="font-size:3rem">🤖</div>
    <div style="font-weight:700;font-size:1.1rem;margin:10px 0">No strategies active</div>
    <div style="color:#6b7280">Go to <b>Strategy Manager</b> to add one.</div>
    </div>
    """, unsafe_allow_html=True)
else:
    for run in runs:
        chip_cls = {
            "WAITING": "chip-wait", "ACTIVE": "chip-on",
            "EXITING": "chip-exit", "DONE": "chip-done", "ERROR": "chip-err",
        }.get(run.state, "chip-wait")
        pnl_clr = "#065f46" if run.pnl >= 0 else "#dc2626"
        mode    = "📄 Paper" if run.paper else "🔴 LIVE"

        with st.container(border=True):
            h1, h2, h3, h4, h5 = st.columns([3, 1.5, 1.5, 1.5, 1])
            h1.markdown(
                f"<b style='font-size:1rem'>{run.name}</b> "
                f"<code style='font-size:.8rem'>{run.id}</code><br>"
                f"<span style='font-size:.8rem;color:#6b7280'>"
                f"{run.symbol} · {run.lots} lot{'s' if run.lots>1 else ''} · {mode}</span>",
                unsafe_allow_html=True,
            )
            h2.markdown(
                f"<div style='font-size:.72rem;color:#6b7280'>P&L</div>"
                f"<div style='font-weight:800;color:{pnl_clr};font-size:1.05rem'>"
                f"₹{run.pnl:+,.0f}</div>", unsafe_allow_html=True)
            h3.markdown(
                f"<div style='font-size:.72rem;color:#6b7280'>State</div>"
                f"<span class='chip {chip_cls}'>{run.state}</span>",
                unsafe_allow_html=True)
            h4.markdown(
                f"<div style='font-size:.72rem;color:#6b7280'>Legs</div>"
                f"<div style='font-weight:600'>{len(run.legs)}</div>",
                unsafe_allow_html=True)
            if h5.button("✕", key=f"del_{run.id}"):
                bot.remove_run(run.id); st.rerun()

            # Legs table
            if run.legs:
                rows = []
                for leg in run.legs:
                    cur    = feed.spot(leg["sym"]) or leg["entry_px"]
                    closed = leg.get("exit_px") is not None
                    leg_pnl = leg.get("pnl") if closed else (
                        (leg["entry_px"] - cur) * leg["qty"] if leg["side"] == "SELL"
                        else (cur - leg["entry_px"]) * leg["qty"]
                    )
                    rows.append({
                        "Symbol":  leg["sym"],
                        "Side":    leg["side"],
                        "Qty":     leg["qty"],
                        "Entry ₹": f"₹{leg['entry_px']:.2f}",
                        "LTP ₹":   f"₹{leg.get('exit_px', cur):.2f}",
                        "P&L":     f"₹{leg_pnl:+,.0f}",
                        "Status":  "Closed" if closed else "Open",
                    })
                st.dataframe(pd.DataFrame(rows), use_container_width=True,
                             hide_index=True, height=min(200, 56 + 38 * len(rows)))

            # Log
            if run.log:
                with st.expander(f"📋 Log ({len(run.log)})", expanded=run.state == "ACTIVE"):
                    st.code("\n".join(f"[{e['ts']}] {e['msg']}"
                                     for e in reversed(run.log[-40:])), language="")

# ── Completed P&L chart ────────────────────────────────────────────────────────
done = [r for r in runs if r.state == "DONE"]
if done:
    st.divider()
    st.subheader("📈 Completed Runs")
    labels = [f"{r.name[:6]}\n{r.symbol} {r.id}" for r in done]
    vals   = [r.pnl for r in done]
    fig    = go.Figure(go.Bar(
        x=labels, y=vals,
        marker_color=["#1a7f3c" if v >= 0 else "#dc2626" for v in vals],
        text=[f"₹{v:+,.0f}" for v in vals], textposition="outside",
    ))
    fig.update_layout(height=260, margin=dict(l=8, r=8, t=16, b=8),
                      yaxis_title="P&L (₹)", plot_bgcolor="#fff",
                      paper_bgcolor="#fff", font=dict(color="#374151"),
                      xaxis=dict(gridcolor="#e5e7eb"),
                      yaxis=dict(gridcolor="#e5e7eb"))
    fig.add_hline(y=0, line_color="#9ca3af", line_width=1)
    st.plotly_chart(fig, use_container_width=True)

# ── Auto-refresh ───────────────────────────────────────────────────────────────
if bot.is_running:
    st.caption(f"🔄 Refreshing every 5s · {datetime.now().strftime('%H:%M:%S')}")
    time.sleep(5)
    st.rerun()
else:
    st.button("🔄 Refresh", key="manual_refresh", on_click=st.rerun)
