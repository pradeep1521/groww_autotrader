"""
Trade History — Full journal from SQLite with analytics.
"""

from datetime import date

import pandas as pd
import plotly.express as px
import plotly.graph_objects as go
import streamlit as st

from data import db

st.set_page_config(page_title="Trade History", page_icon="📋", layout="wide")
st.title("📋 Trade History")

trades = db.get_trades(limit=1000)

if not trades:
    st.info("No trades recorded yet. Add a strategy and run the bot!")
    st.stop()

df = pd.DataFrame(trades)

# ── Convert & derive ───────────────────────────────────────────────────────────
df["entry_time"] = pd.to_datetime(df["entry_time"], errors="coerce")
df["exit_time"]  = pd.to_datetime(df["exit_time"],  errors="coerce")
df["date"]       = df["entry_time"].dt.date
df["pnl"]        = pd.to_numeric(df["pnl"], errors="coerce").fillna(0)
df["entry_px"]   = pd.to_numeric(df["entry_px"], errors="coerce")
df["exit_px"]    = pd.to_numeric(df["exit_px"],  errors="coerce")
df["paper"]      = df["paper"].astype(bool)

# ── Filters ────────────────────────────────────────────────────────────────────
with st.expander("🔍 Filters", expanded=False):
    fc1, fc2, fc3, fc4 = st.columns(4)
    strats = ["All"] + sorted(df["strategy"].dropna().unique().tolist())
    syms   = ["All"] + sorted(df["symbol"].dropna().unique().tolist())
    modes  = ["All", "Paper", "Live"]
    statuses = ["All"] + sorted(df["status"].dropna().unique().tolist())

    f_strat  = fc1.selectbox("Strategy", strats, key="h_st")
    f_sym    = fc2.selectbox("Symbol",   syms,   key="h_sy")
    f_mode   = fc3.selectbox("Mode",     modes,  key="h_mo")
    f_status = fc4.selectbox("Status",   statuses, key="h_ss")

    date_min = df["date"].dropna().min() or date.today()
    date_max = df["date"].dropna().max() or date.today()
    d1, d2 = st.columns(2)
    f_from = d1.date_input("From", date_min, key="h_df")
    f_to   = d2.date_input("To",   date_max, key="h_dt")

dff = df.copy()
if f_strat  != "All":  dff = dff[dff["strategy"] == f_strat]
if f_sym    != "All":  dff = dff[dff["symbol"]   == f_sym]
if f_mode   == "Paper": dff = dff[dff["paper"] == True]
if f_mode   == "Live":  dff = dff[dff["paper"] == False]
if f_status != "All":  dff = dff[dff["status"] == f_status]
dff = dff[dff["date"].between(f_from, f_to)]

# ── KPI row ────────────────────────────────────────────────────────────────────
closed  = dff[dff["status"] == "CLOSED"]
winners = closed[closed["pnl"] > 0]
losers  = closed[closed["pnl"] < 0]
gross_p = winners["pnl"].sum()
gross_l = losers["pnl"].sum()
net_pnl = gross_p + gross_l
win_rt  = len(winners) / len(closed) * 100 if len(closed) else 0
avg_w   = winners["pnl"].mean() if len(winners) else 0
avg_l   = losers["pnl"].mean()  if len(losers)  else 0
profit_f = abs(gross_p / gross_l) if gross_l != 0 else float("inf")

ka, kb, kc, kd, ke, kf = st.columns(6)
def _kpi(col, lbl, val):
    col.metric(lbl, val)

_kpi(ka, "Total trades",   f"{len(dff)}")
_kpi(kb, "Net P&L",        f"₹{net_pnl:+,.0f}")
_kpi(kc, "Win rate",       f"{win_rt:.0f}%")
_kpi(kd, "Avg winner",     f"₹{avg_w:,.0f}")
_kpi(ke, "Avg loser",      f"₹{avg_l:,.0f}")
_kpi(kf, "Profit factor",  f"{profit_f:.2f}" if profit_f != float("inf") else "∞")

st.divider()

# ── Charts ─────────────────────────────────────────────────────────────────────
ch1, ch2 = st.columns(2)

# Daily P&L
with ch1:
    st.subheader("📈 Daily P&L")
    daily = closed.groupby("date")["pnl"].sum().reset_index()
    daily.columns = ["date", "pnl"]
    daily["cum"]  = daily["pnl"].cumsum()
    fig = go.Figure()
    fig.add_bar(x=daily["date"], y=daily["pnl"],
                marker_color=["#1a7f3c" if v >= 0 else "#dc2626" for v in daily["pnl"]],
                name="Daily P&L")
    fig.add_scatter(x=daily["date"], y=daily["cum"],
                    mode="lines", name="Cumulative",
                    line=dict(color="#2563eb", width=2))
    fig.update_layout(height=300, margin=dict(l=4, r=4, t=8, b=4),
                      legend=dict(orientation="h", y=1.1),
                      paper_bgcolor="#fff", plot_bgcolor="#fff",
                      yaxis=dict(gridcolor="#e5e7eb"),
                      xaxis=dict(gridcolor="#e5e7eb"))
    fig.add_hline(y=0, line_color="#9ca3af", line_width=1)
    st.plotly_chart(fig, use_container_width=True)

# P&L by strategy
with ch2:
    st.subheader("🏷 P&L by Strategy")
    by_strat = closed.groupby("strategy")["pnl"].sum().reset_index()
    fig2 = go.Figure(go.Bar(
        x=by_strat["strategy"],
        y=by_strat["pnl"],
        marker_color=["#1a7f3c" if v >= 0 else "#dc2626" for v in by_strat["pnl"]],
        text=[f"₹{v:+,.0f}" for v in by_strat["pnl"]],
        textposition="outside",
    ))
    fig2.update_layout(height=300, margin=dict(l=4, r=4, t=8, b=4),
                       paper_bgcolor="#fff", plot_bgcolor="#fff",
                       yaxis=dict(gridcolor="#e5e7eb"),
                       xaxis=dict(gridcolor="#e5e7eb"))
    fig2.add_hline(y=0, line_color="#9ca3af", line_width=1)
    st.plotly_chart(fig2, use_container_width=True)

# Equity curve + drawdown
if len(closed) >= 2:
    st.subheader("📉 Equity Curve & Drawdown")
    ec = closed.sort_values("exit_time").copy()
    ec["cumPnl"] = ec["pnl"].cumsum()
    ec["peak"]   = ec["cumPnl"].cummax()
    ec["dd"]     = ec["cumPnl"] - ec["peak"]
    fig3 = go.Figure()
    fig3.add_scatter(x=ec["exit_time"], y=ec["cumPnl"], name="Equity",
                     line=dict(color="#2563eb", width=2), fill="tozeroy",
                     fillcolor="rgba(37,99,235,.08)")
    fig3.add_scatter(x=ec["exit_time"], y=ec["dd"],    name="Drawdown",
                     line=dict(color="#dc2626", width=1.5, dash="dash"))
    fig3.update_layout(height=280, margin=dict(l=4, r=4, t=8, b=4),
                       legend=dict(orientation="h", y=1.12),
                       paper_bgcolor="#fff", plot_bgcolor="#fff",
                       yaxis=dict(gridcolor="#e5e7eb"),
                       xaxis=dict(gridcolor="#e5e7eb"))
    fig3.add_hline(y=0, line_color="#9ca3af", line_width=1)
    st.plotly_chart(fig3, use_container_width=True)

# ── Trade table ────────────────────────────────────────────────────────────────
st.subheader(f"🗃 Trades ({len(dff):,})")
display_cols = ["id", "strategy", "symbol", "side", "qty",
                "entry_px", "exit_px", "pnl", "status", "paper", "entry_time", "exit_time"]
display_df = dff[[c for c in display_cols if c in dff.columns]].copy()
display_df["paper"] = display_df["paper"].map({True: "Paper", False: "Live"})
display_df.rename(columns={
    "id": "ID", "strategy": "Strategy", "symbol": "Symbol", "side": "Side",
    "qty": "Qty", "entry_px": "Entry ₹", "exit_px": "Exit ₹", "pnl": "P&L",
    "status": "Status", "paper": "Mode", "entry_time": "Entry Time", "exit_time": "Exit Time",
}, inplace=True)

st.dataframe(
    display_df.sort_values("Entry Time", ascending=False),
    use_container_width=True,
    hide_index=True,
    height=450,
    column_config={
        "P&L": st.column_config.NumberColumn("P&L (₹)", format="₹%.0f"),
        "Entry ₹": st.column_config.NumberColumn(format="₹%.2f"),
        "Exit ₹":  st.column_config.NumberColumn(format="₹%.2f"),
    },
)

# Export
csv = display_df.to_csv(index=False).encode()
st.download_button("⬇️ Export CSV", csv, "trades.csv", "text/csv", key="h_dl")
