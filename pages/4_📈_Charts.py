"""
Live Stock Charts
=================
Candlestick chart with EMA, Bollinger Bands, Volume, RSI, MACD.
Screener score card. One-click trade setup.
"""

import pandas as pd
import plotly.graph_objects as go
from plotly.subplots import make_subplots
import streamlit as st

from broker.groww import connector
from data import feed
from engine.bot import bot
from engine.indicators import (
    bollinger_bands, ema as _ema, macd as _macd, rsi as _rsi,
    atr as _atr, adx as _adx, volume_ratio as _vol_ratio,
)
from engine.risk_guard import risk_guard
from engine.screener import NIFTY50, NIFTY_NEXT50, FNO_EXTRAS, screener

st.set_page_config(page_title="Live Charts", page_icon="📈", layout="wide")

# ── CSS ─────────────────────────────────────────────────────────────────────────
st.markdown("""
<style>
.price-big { font-size:2.2rem; font-weight:900; line-height:1.1; }
.chg-pos   { color:#065f46; font-size:1.1rem; font-weight:700; }
.chg-neg   { color:#dc2626; font-size:1.1rem; font-weight:700; }
.score-box { border-radius:8px; padding:10px 16px; text-align:center; margin:4px 0; }
.kpi       { background:#f9fafb; border:1px solid #e5e7eb; border-radius:8px;
             padding:10px 14px; text-align:center; }
.kpi-lbl   { font-size:.72rem; color:#6b7280; }
.kpi-num   { font-size:1.1rem; font-weight:800; }
</style>
""", unsafe_allow_html=True)

# ── Symbol selection ─────────────────────────────────────────────────────────────
ALL_SYMBOLS = sorted(set(NIFTY50 + NIFTY_NEXT50 + FNO_EXTRAS) - {"NIFTY","BANKNIFTY","FINNIFTY","MIDCAPNIFTY"})

col_sym, col_tf, col_int, col_ref = st.columns([3, 2, 2, 1])

with col_sym:
    # Pre-select from session state (set by screener tab "Chart" buttons)
    default_sym = st.session_state.get("chart_symbol", "RELIANCE")
    default_idx = ALL_SYMBOLS.index(default_sym) if default_sym in ALL_SYMBOLS else 0
    symbol = st.selectbox("Stock", ALL_SYMBOLS, index=default_idx, key="chart_sym_sel")
    st.session_state["chart_symbol"] = symbol

with col_tf:
    period = st.selectbox("Period", ["1d", "5d", "1mo", "3mo", "6mo", "1y"],
                          index=1, key="chart_period")

with col_int:
    interval_map = {
        "1d":  ["5m", "15m", "30m", "1h"],
        "5d":  ["15m", "30m", "1h"],
        "1mo": ["1h", "1d"],
        "3mo": ["1d", "1wk"],
        "6mo": ["1d", "1wk"],
        "1y":  ["1d", "1wk"],
    }
    interval = st.selectbox("Interval", interval_map.get(period, ["1d"]),
                            key="chart_interval")

with col_ref:
    st.write("")
    refresh_btn = st.button("🔄", help="Refresh", use_container_width=True)

# ── Overlays ────────────────────────────────────────────────────────────────────
ov1, ov2, ov3, ov4, ov5 = st.columns(5)
show_ema9  = ov1.toggle("EMA 9",  value=True,  key="ov_e9")
show_ema21 = ov2.toggle("EMA 21", value=True,  key="ov_e21")
show_bb    = ov3.toggle("BB",     value=True,  key="ov_bb")
show_rsi   = ov4.toggle("RSI",    value=True,  key="ov_rsi")
show_macd  = ov5.toggle("MACD",   value=False, key="ov_macd")

st.divider()

# ── Fetch data ───────────────────────────────────────────────────────────────────
with st.spinner(f"Loading {symbol}…"):
    df = feed.ohlcv(symbol, period=period, interval=interval)
    q  = feed.refresh(symbol)   # live price

if df is None or df.empty:
    st.error(f"Could not load data for **{symbol}**. Check symbol or try a different period/interval.")
    st.stop()

closes  = list(df["Close"])
highs   = list(df["High"])
lows    = list(df["Low"])
volumes = list(df["Volume"])

# ── Live price header ────────────────────────────────────────────────────────────
price   = q.get("price",      closes[-1])   if q else closes[-1]
chg     = q.get("change",     0.0)          if q else 0.0
chg_pct = q.get("change_pct", 0.0)          if q else 0.0
prev    = q.get("prev_close", closes[-2] if len(closes) > 1 else price) if q else price

ph1, ph2, ph3 = st.columns([3, 4, 3])
with ph1:
    chg_cls = "chg-pos" if chg >= 0 else "chg-neg"
    arrow   = "▲" if chg >= 0 else "▼"
    st.markdown(
        f"<div class='price-big'>₹{price:,.2f}</div>"
        f"<div class='{chg_cls}'>{arrow} ₹{chg:+.2f} ({chg_pct:+.2f}%)</div>"
        f"<div style='font-size:.8rem;color:#6b7280'>Prev close ₹{prev:,.2f}</div>",
        unsafe_allow_html=True,
    )

# Day OHLV from today's data
if period == "1d":
    d_open  = df["Open"].iloc[0]
    d_high  = df["High"].max()
    d_low   = df["Low"].min()
    d_vol   = df["Volume"].sum()
    k1, k2, k3, k4 = ph2.columns(4)
    k1.markdown(f"<div class='kpi'><div class='kpi-lbl'>Open</div>"
                f"<div class='kpi-num'>₹{d_open:,.0f}</div></div>", unsafe_allow_html=True)
    k2.markdown(f"<div class='kpi'><div class='kpi-lbl'>High</div>"
                f"<div class='kpi-num' style='color:#065f46'>₹{d_high:,.0f}</div></div>",
                unsafe_allow_html=True)
    k3.markdown(f"<div class='kpi'><div class='kpi-lbl'>Low</div>"
                f"<div class='kpi-num' style='color:#dc2626'>₹{d_low:,.0f}</div></div>",
                unsafe_allow_html=True)
    k4.markdown(f"<div class='kpi'><div class='kpi-lbl'>Volume</div>"
                f"<div class='kpi-num'>{d_vol/1e6:.1f}M</div></div>", unsafe_allow_html=True)

# ── Compute indicators ───────────────────────────────────────────────────────────
e9   = [_ema(closes[:i], 9)  for i in range(1, len(closes) + 1)]
e21  = [_ema(closes[:i], 21) for i in range(1, len(closes) + 1)]
bb_data = [bollinger_bands(closes[:i], 20, 2.0) for i in range(1, len(closes) + 1)]
bb_up  = [b[0] for b in bb_data]
bb_mid = [b[1] for b in bb_data]
bb_low = [b[2] for b in bb_data]

rsi_vals  = [_rsi(closes[:i], 14) for i in range(1, len(closes) + 1)]
macd_line = [_macd(closes[:i], 12, 26, 9)[0] for i in range(1, len(closes) + 1)]
macd_sig  = [_macd(closes[:i], 12, 26, 9)[1] for i in range(1, len(closes) + 1)]
macd_hist = [_macd(closes[:i], 12, 26, 9)[2] for i in range(1, len(closes) + 1)]

# ── Build chart ──────────────────────────────────────────────────────────────────
n_rows   = 2 + (1 if show_rsi else 0) + (1 if show_macd else 0)
row_heights_base = [0.55, 0.15]
if show_rsi:  row_heights_base.append(0.15)
if show_macd: row_heights_base.append(0.15)
total = sum(row_heights_base)
row_heights = [h / total for h in row_heights_base]

subplot_titles = ["Price", "Volume"]
if show_rsi:  subplot_titles.append("RSI (14)")
if show_macd: subplot_titles.append("MACD (12,26,9)")

fig = make_subplots(
    rows=n_rows, cols=1,
    shared_xaxes=True,
    row_heights=row_heights,
    vertical_spacing=0.03,
    subplot_titles=subplot_titles,
)

idx = df.index

# ── Candlestick ──────────────────────────────────────────────────────────────────
fig.add_trace(go.Candlestick(
    x=idx,
    open=df["Open"], high=df["High"], low=df["Low"], close=df["Close"],
    name=symbol,
    increasing_line_color="#1a7f3c", decreasing_line_color="#dc2626",
    increasing_fillcolor="#d1fae5",  decreasing_fillcolor="#fee2e2",
), row=1, col=1)

if show_ema9:
    fig.add_trace(go.Scatter(x=idx, y=e9, name="EMA 9",
                             line=dict(color="#f59e0b", width=1.5)), row=1, col=1)
if show_ema21:
    fig.add_trace(go.Scatter(x=idx, y=e21, name="EMA 21",
                             line=dict(color="#6366f1", width=1.5)), row=1, col=1)
if show_bb:
    fig.add_trace(go.Scatter(x=idx, y=bb_up,  name="BB Upper",
                             line=dict(color="#94a3b8", width=1, dash="dash")), row=1, col=1)
    fig.add_trace(go.Scatter(x=idx, y=bb_mid, name="BB Mid",
                             line=dict(color="#cbd5e1", width=1)), row=1, col=1)
    fig.add_trace(go.Scatter(x=idx, y=bb_low, name="BB Lower",
                             line=dict(color="#94a3b8", width=1, dash="dash"),
                             fill="tonexty", fillcolor="rgba(148,163,184,0.05)"), row=1, col=1)

# ── Volume ───────────────────────────────────────────────────────────────────────
vol_colors = ["#d1fae5" if c >= o else "#fee2e2"
              for c, o in zip(df["Close"], df["Open"])]
fig.add_trace(go.Bar(x=idx, y=df["Volume"], name="Volume",
                     marker_color=vol_colors, showlegend=False), row=2, col=1)

# ── RSI ──────────────────────────────────────────────────────────────────────────
rsi_row = 3
if show_rsi:
    fig.add_trace(go.Scatter(x=idx, y=rsi_vals, name="RSI",
                             line=dict(color="#7c3aed", width=1.5)), row=rsi_row, col=1)
    fig.add_hline(y=70, line_color="#dc2626", line_dash="dot",
                  line_width=1, row=rsi_row, col=1)
    fig.add_hline(y=30, line_color="#065f46", line_dash="dot",
                  line_width=1, row=rsi_row, col=1)
    fig.update_yaxes(range=[0, 100], row=rsi_row, col=1)

# ── MACD ─────────────────────────────────────────────────────────────────────────
macd_row = rsi_row + (1 if show_rsi else 0)
if show_macd:
    hist_colors = ["#1a7f3c" if (h or 0) >= 0 else "#dc2626" for h in macd_hist]
    fig.add_trace(go.Bar(x=idx, y=macd_hist, name="MACD Hist",
                         marker_color=hist_colors, showlegend=False), row=macd_row, col=1)
    fig.add_trace(go.Scatter(x=idx, y=macd_line, name="MACD",
                             line=dict(color="#0ea5e9", width=1.5)), row=macd_row, col=1)
    fig.add_trace(go.Scatter(x=idx, y=macd_sig, name="Signal",
                             line=dict(color="#f97316", width=1.5)), row=macd_row, col=1)

# ── Layout ───────────────────────────────────────────────────────────────────────
fig.update_layout(
    height=600,
    margin=dict(l=8, r=8, t=32, b=8),
    plot_bgcolor="#ffffff",
    paper_bgcolor="#ffffff",
    font=dict(color="#374151", size=11),
    legend=dict(orientation="h", y=1.02, x=0),
    xaxis_rangeslider_visible=False,
    hovermode="x unified",
)
for i in range(1, n_rows + 1):
    fig.update_xaxes(gridcolor="#f1f5f9", row=i, col=1)
    fig.update_yaxes(gridcolor="#f1f5f9", row=i, col=1)

st.plotly_chart(fig, use_container_width=True)

# ── Screener score card + trade setup ────────────────────────────────────────────
scr_data = screener.get_results().get(symbol.upper())
vix      = feed.spot("VIX") or 15

st.divider()
sc_col, tr_col = st.columns([2, 3])

with sc_col:
    st.markdown("#### 📊 Screener Analysis")
    if scr_data:
        rsi14    = scr_data.get("rsi", _rsi(closes, 14) or 50)
        atr14    = scr_data.get("atr_pct", 1.5)
        mom      = scr_data.get("mom_score", 0)
        rev      = scr_data.get("rev_score", 0)
        vol_r    = scr_data.get("vol_ratio", 1.0)
        adx14    = scr_data.get("adx", 0)
        sig      = scr_data.get("signal", "—")
        pct_52h  = scr_data.get("pct_from_52h", 0)
    else:
        # Compute on the fly from the loaded data
        rsi14  = _rsi(closes, 14) or 50
        atr14  = ((_atr(highs, lows, closes, 14) or price * 0.015) / price * 100)
        vol_r  = _vol_ratio(volumes, 20) or 1.0
        adx14  = _adx(highs, lows, closes, 14) or 0
        mom    = 0
        rev    = 0
        sig    = "—"
        pct_52h = 0

    m1, m2, m3, m4 = st.columns(4)
    m1.markdown(f"<div class='kpi'><div class='kpi-lbl'>RSI</div>"
                f"<div class='kpi-num' style='color:{'#dc2626' if rsi14>70 else '#065f46' if rsi14<30 else '#374151'}'>"
                f"{rsi14:.0f}</div></div>", unsafe_allow_html=True)
    m2.markdown(f"<div class='kpi'><div class='kpi-lbl'>ADX</div>"
                f"<div class='kpi-num' style='color:{'#065f46' if adx14 and adx14>25 else '#6b7280'}'>"
                f"{adx14:.0f if adx14 else '—'}</div></div>", unsafe_allow_html=True)
    m3.markdown(f"<div class='kpi'><div class='kpi-lbl'>Vol Ratio</div>"
                f"<div class='kpi-num' style='color:{'#065f46' if vol_r and vol_r>1.5 else '#374151'}'>"
                f"{vol_r:.1f}×</div></div>", unsafe_allow_html=True)
    m4.markdown(f"<div class='kpi'><div class='kpi-lbl'>ATR %</div>"
                f"<div class='kpi-num'>{atr14:.2f}%</div></div>", unsafe_allow_html=True)

    st.write("")
    s1, s2 = st.columns(2)
    s1.markdown(
        f"<div class='score-box' style='background:#d1fae5'>"
        f"<div style='font-size:.72rem;color:#065f46'>🚀 Momentum</div>"
        f"<div style='font-size:1.8rem;font-weight:900;color:#065f46'>{mom}</div>"
        f"<div style='height:6px;background:#a7f3d0;border-radius:3px'>"
        f"<div style='height:6px;width:{mom}%;background:#059669;border-radius:3px'></div></div>"
        f"</div>", unsafe_allow_html=True)
    s2.markdown(
        f"<div class='score-box' style='background:#fce7f3'>"
        f"<div style='font-size:.72rem;color:#9d174d'>📉 Reversion</div>"
        f"<div style='font-size:1.8rem;font-weight:900;color:#9d174d'>{rev}</div>"
        f"<div style='height:6px;background:#fbcfe8;border-radius:3px'>"
        f"<div style='height:6px;width:{rev}%;background:#ec4899;border-radius:3px'></div></div>"
        f"</div>", unsafe_allow_html=True)

    if sig != "—":
        st.info(f"**Best fit:** {sig} strategy · {pct_52h:+.1f}% from 52-week high")

with tr_col:
    st.markdown("#### 🚀 Trade Setup")
    atr_val   = price * atr14 / 100
    open_cnt  = sum(1 for r in bot.get_runs() if r.state in ("WAITING", "ACTIVE"))
    live_mode = connector.is_connected

    t1, t2 = st.columns(2)

    # ── Intraday setup ─────────────────────────────────────────────────────────
    with t1:
        st.markdown("**⚡ Intraday (MIS)**")
        intr_mode = st.selectbox("Mode", ["Momentum", "VWAP", "ORB"], key="ct_imode")
        intr_qty  = st.number_input(
            "Qty", 1, 50000,
            max(1, risk_guard.position_size(price, atr_val, vix, open_cnt)),
            key="ct_iqty",
            help="Auto-sized by ATR risk"
        )
        intr_sl   = st.number_input("SL %", 0.1, 5.0,
                                     round(risk_guard.sl_pct(price, atr_val, vix), 2),
                                     step=0.1, key="ct_isl")
        intr_tgt  = st.number_input("Target %", 0.1, 10.0,
                                     round(risk_guard.target_pct(price, atr_val, vix), 2),
                                     step=0.1, key="ct_itgt")
        intr_paper = not live_mode or st.toggle("Paper mode", True, key="ct_ipaper")

        # Risk summary
        max_loss = round(price * intr_sl / 100 * intr_qty, 0)
        max_gain = round(price * intr_tgt / 100 * intr_qty, 0)
        rr       = round(intr_tgt / intr_sl, 2) if intr_sl > 0 else 0
        st.markdown(
            f"<div style='font-size:.8rem;color:#6b7280'>"
            f"Max loss ₹{max_loss:,.0f} · Max gain ₹{max_gain:,.0f} · R:R {rr:.1f}:1</div>",
            unsafe_allow_html=True,
        )
        if st.button("⚡ Add Intraday run", type="primary",
                     use_container_width=True, key="ct_iadd"):
            sid = bot.add_run("Intraday", symbol=symbol, paper=intr_paper, params={
                "symbol": symbol, "mode": intr_mode,
                "qty": intr_qty, "entry_time": "09:20", "exit_time": "15:10",
                "target_pct": intr_tgt, "sl_pct": intr_sl,
                "fast_ema": 9, "slow_ema": 21,
            })
            if not bot.is_running: bot.start()
            st.success(f"✅ Intraday run `{sid}` added for {symbol}")

    # ── MTF setup ──────────────────────────────────────────────────────────────
    with t2:
        st.markdown("**💳 MTF (Swing)**")
        mtf_sig  = st.selectbox("Signal", ["EMA Cross", "RSI Bounce"], key="ct_msig")
        mtf_qty  = st.number_input(
            "Qty", 1, 50000,
            max(1, risk_guard.position_size(price, atr_val, vix, open_cnt)),
            key="ct_mqty",
            help="Auto-sized by ATR risk"
        )
        mtf_sl   = st.number_input("SL %", 0.1, 10.0,
                                    round(risk_guard.sl_pct(price, atr_val, vix), 2),
                                    step=0.1, key="ct_msl")
        mtf_tgt  = st.number_input("Target %", 0.1, 20.0,
                                    round(risk_guard.target_pct(price, atr_val, vix), 2),
                                    step=0.1, key="ct_mtgt")
        mtf_days = st.number_input("Max days", 1, 30, 3, key="ct_mdays")
        mtf_paper = not live_mode or st.toggle("Paper mode", True, key="ct_mpaper")

        max_loss_m = round(price * mtf_sl / 100 * mtf_qty, 0)
        max_gain_m = round(price * mtf_tgt / 100 * mtf_qty, 0)
        rr_m       = round(mtf_tgt / mtf_sl, 2) if mtf_sl > 0 else 0
        st.markdown(
            f"<div style='font-size:.8rem;color:#6b7280'>"
            f"Max loss ₹{max_loss_m:,.0f} · Max gain ₹{max_gain_m:,.0f} · R:R {rr_m:.1f}:1</div>",
            unsafe_allow_html=True,
        )
        if st.button("💳 Add MTF run", type="primary",
                     use_container_width=True, key="ct_madd"):
            sid = bot.add_run("MTF", symbol=symbol, paper=mtf_paper, params={
                "symbol": symbol, "signal": mtf_sig,
                "fast_ema": 9, "slow_ema": 21, "rsi_level": 35,
                "qty": mtf_qty, "target_pct": mtf_tgt,
                "sl_pct": mtf_sl, "max_days": mtf_days,
            })
            if not bot.is_running: bot.start()
            st.success(f"✅ MTF run `{sid}` added for {symbol}")
