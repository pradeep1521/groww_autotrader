"""
Groww Signal Dashboard
======================
Shows live BUY/SELL signals for stocks and NIFTY/BANKNIFTY options.
Notifies you (toast + sound) when a new signal fires — you place the order on Groww.

No jargon. No bot controls. Just signals.
"""

import time

import streamlit as st
import streamlit.components.v1 as components

from broker.groww import connector
from data import db, feed
from engine.screener import screener

st.set_page_config(
    page_title="Signal Dashboard",
    page_icon="📡",
    layout="wide",
    initial_sidebar_state="expanded",
)

# ── CSS ────────────────────────────────────────────────────────────────────────
st.markdown("""
<style>
.sig-card {
    border-radius: 14px;
    padding: 18px 22px;
    margin-bottom: 12px;
}
.sig-buy  { background: #f0fdf4; border: 2px solid #16a34a; }
.sig-sell { background: #fff1f2; border: 2px solid #e11d48; }
.sig-neu  { background: #f8fafc; border: 2px solid #94a3b8; }

.mkt {
    background: #f9fafb;
    border: 1px solid #e5e7eb;
    border-radius: 12px;
    padding: 16px 20px;
    text-align: center;
}

.tag {
    display: inline-block;
    border-radius: 5px;
    padding: 2px 10px;
    font-size: .76rem;
    font-weight: 700;
    letter-spacing: .02em;
}
.tag-buy  { background: #dcfce7; color: #166534; }
.tag-sell { background: #ffe4e6; color: #9f1239; }
.tag-neu  { background: #e0f2fe; color: #075985; }
.tag-new  { background: #fef9c3; color: #713f12; }

.stat-row { display: flex; gap: 28px; margin-top: 12px; flex-wrap: wrap; }
.stat-col { display: flex; flex-direction: column; }
.stat-lbl { font-size: .68rem; color: #64748b; font-weight: 600;
            text-transform: uppercase; letter-spacing: .04em; margin-bottom: 2px; }
.stat-val { font-size: 1.05rem; font-weight: 700; }
.val-sl   { color: #e11d48; }
.val-tgt  { color: #16a34a; }
</style>
""", unsafe_allow_html=True)


# ── Sidebar ────────────────────────────────────────────────────────────────────
with st.sidebar:
    st.markdown("## 📡 Signal Dashboard")
    st.divider()

    if connector.is_connected:
        st.success("🟢 Connected to Groww")
        try:
            m = connector.margin()
            st.metric("Available margin", f"₹{m.get('available', 0):,.0f}")
        except Exception:
            pass
    else:
        st.info("🔵 Not connected — **paper mode**")
        st.page_link("pages/3_🔑_Broker_Connect.py", label="🔑 Connect to Groww →")

    st.divider()
    st.caption("PAGES")
    st.page_link("app.py",                           label="📡 Signals")
    st.page_link("pages/2_📋_History.py",            label="📋 Trade History")
    st.page_link("pages/3_🔑_Broker_Connect.py",     label="🔑 Broker Connect")
    st.page_link("pages/4_📈_Charts.py",             label="📈 Charts")

    st.divider()
    st.caption("SETTINGS")
    sound_on  = st.toggle("🔔 Sound alert on new signal", value=True,  key="snd")
    notif_on  = st.toggle("🖥 Browser notifications",     value=False, key="notif")
    auto_scan = st.toggle("♻️ Auto-scan every 15 min",   value=True,  key="auto")


# ── Page header ────────────────────────────────────────────────────────────────
st.title("📡 Signal Dashboard")
st.caption(
    "Scans Nifty 50 stocks and the options chain every 15 minutes. "
    "When a signal fires you will be notified — place the order yourself on Groww."
)


# ── Market snapshot ────────────────────────────────────────────────────────────
c1, c2, c3 = st.columns(3)
for col, sym, label in [
    (c1, "NIFTY",     "NIFTY 50"),
    (c2, "BANKNIFTY", "BANK NIFTY"),
    (c3, "VIX",       "India VIX"),
]:
    q   = feed.get_price(sym) or {}
    px  = q.get("price", 0.0)
    chg = q.get("change_pct", 0.0)
    clr = "#16a34a" if chg >= 0 else "#e11d48"
    arr = "▲" if chg >= 0 else "▼"
    col.markdown(
        f"<div class='mkt'>"
        f"<div style='font-size:.72rem;color:#6b7280;font-weight:600'>{label}</div>"
        f"<div style='font-size:1.6rem;font-weight:800;line-height:1.2'>₹{px:,.2f}</div>"
        f"<div style='font-size:.9rem;color:{clr};font-weight:700;margin-top:2px'>"
        f"{arr} {abs(chg):.2f}%</div>"
        f"</div>",
        unsafe_allow_html=True,
    )

vix = feed.spot("VIX") or 0.0
st.write("")
if vix >= 25:
    st.error(
        f"⚠️ **VIX {vix:.1f} — Very high volatility.**  "
        "Consider smaller position sizes or wait for VIX to cool below 22."
    )
elif vix >= 20:
    st.warning(f"⚠️ **VIX {vix:.1f} — Elevated risk.**  Use smaller position sizes.")

st.divider()


# ── Run screener if needed ─────────────────────────────────────────────────────
if screener.last_scan is None:
    with st.spinner("📊 Scanning Nifty 50 stocks for signals — takes about 25 s ..."):
        screener.universe = "nifty50"
        screener.scan()
elif auto_scan and not screener._running:
    screener.universe = "nifty50"
    screener.start()   # background thread — scans every 15 min


# ── Build signals (reads from screener cache — instant) ────────────────────────
from engine.signal_builder import options_signals, stock_signals  # noqa: E402

sigs     = stock_signals(8)
opt_sigs = options_signals()


# ── Detect new signals and notify ─────────────────────────────────────────────
sig_keys  = frozenset(f"{s['symbol']}_{s['direction']}" for s in sigs)
prev_keys = st.session_state.get("_prev_sig_keys", frozenset())
new_keys  = sig_keys - prev_keys
st.session_state["_prev_sig_keys"] = sig_keys

if new_keys:
    for k in new_keys:
        st.toast(f"🔔 New signal: {k.split('_')[0]}", icon="📡")

    if sound_on:
        components.html(
            """
            <script>
            (function() {
              try {
                var ctx = new (window.AudioContext || window.webkitAudioContext)();
                [880, 1100, 1320].forEach(function(freq, i) {
                  var o = ctx.createOscillator(), g = ctx.createGain();
                  o.type = 'sine'; o.frequency.value = freq;
                  var t = ctx.currentTime + i * 0.18;
                  g.gain.setValueAtTime(0.25, t);
                  g.gain.exponentialRampToValueAtTime(0.001, t + 0.22);
                  o.connect(g); g.connect(ctx.destination);
                  o.start(t); o.stop(t + 0.25);
                });
              } catch(e) {}
            })();
            </script>
            """,
            height=0,
        )

    if notif_on:
        syms_str = ", ".join(k.split("_")[0] for k in new_keys)
        components.html(
            f"""
            <script>
            (function() {{
              if (typeof Notification === 'undefined') return;
              function send() {{
                new Notification('📡 New signal', {{
                  body: '{syms_str}',
                  icon: 'https://img.icons8.com/fluency/48/robot-2.png'
                }});
              }}
              if (Notification.permission === 'granted') {{
                send();
              }} else if (Notification.permission !== 'denied') {{
                Notification.requestPermission().then(function(p) {{
                  if (p === 'granted') send();
                }});
              }}
            }})();
            </script>
            """,
            height=0,
        )


# ── Stock signals ──────────────────────────────────────────────────────────────
st.subheader(f"📈 Stock Signals  ·  {len(sigs)} found")

if not sigs:
    st.info(
        "**No signals right now.**\n\n"
        "This is normal — the market may be closed, or no stock has a clean setup yet. "
        "Click **Scan Now** below or wait for the auto-scan."
    )
else:
    for i, s in enumerate(sigs):
        is_new  = f"{s['symbol']}_{s['direction']}" in new_keys
        is_buy  = s["direction"] == "BUY"
        css     = "sig-buy" if is_buy else "sig-sell"
        emoji   = "🟢" if is_buy else "🔴"
        tag_css = "tag-buy" if is_buy else "tag-sell"
        new_tag = "<span class='tag tag-new'>⚡ NEW</span>&nbsp;" if is_new else ""
        setup   = "Momentum" if s["setup"] == "MOMENTUM" else "Oversold bounce"

        st.markdown(
            f"""
            <div class='sig-card {css}'>
              <div style='display:flex;justify-content:space-between;
                          align-items:flex-start;gap:12px'>
                <div>
                  {new_tag}
                  <span style='font-size:1.2rem;font-weight:800'>
                    {emoji} {s['direction']} &nbsp;
                    <span style='font-family:monospace'>{s['symbol']}</span>
                  </span>
                  &nbsp;<span class='tag {tag_css}'>{setup}</span>
                  <div style='color:#475569;font-size:.84rem;margin-top:5px'>
                    {s['reason']}
                  </div>
                </div>
                <div style='text-align:right;white-space:nowrap'>
                  <div style='font-size:.68rem;color:#94a3b8;font-weight:600'>SIGNAL SCORE</div>
                  <div style='font-size:1.1rem;font-weight:800;color:#334155'>{s['score']:.0f}/100</div>
                </div>
              </div>
              <div class='stat-row'>
                <div class='stat-col'>
                  <span class='stat-lbl'>Entry price</span>
                  <span class='stat-val'>₹{s['price']:,.2f}</span>
                </div>
                <div class='stat-col'>
                  <span class='stat-lbl'>Stop-Loss</span>
                  <span class='stat-val val-sl'>₹{s['sl']:,.2f}</span>
                </div>
                <div class='stat-col'>
                  <span class='stat-lbl'>Target</span>
                  <span class='stat-val val-tgt'>₹{s['target']:,.2f}</span>
                </div>
                <div class='stat-col'>
                  <span class='stat-lbl'>Risk : Reward</span>
                  <span class='stat-val'>1 : {s['risk_reward']}</span>
                </div>
                <div class='stat-col'>
                  <span class='stat-lbl'>RSI</span>
                  <span class='stat-val'>{s['rsi']}</span>
                </div>
              </div>
            </div>
            """,
            unsafe_allow_html=True,
        )

        # ── Place order button for this signal ────────────────────────────
        btn_col, _ = st.columns([1, 5])
        with btn_col:
            with st.popover(f"🛒 Place Order — {s['symbol']}", use_container_width=True):
                is_live = connector.is_connected
                st.markdown(f"**{s['direction']} {s['symbol']}** · Entry ~₹{s['price']:,.2f}")
                st.caption(f"SL ₹{s['sl']:,.2f}  ·  Target ₹{s['target']:,.2f}  ·  R:R 1:{s['risk_reward']}")
                if is_live:
                    st.success("🟢 Connected — will place a REAL order on Groww")
                else:
                    st.info("🔵 Not connected — will be logged as a paper trade")
                qty = st.number_input(
                    "Quantity (shares)", min_value=1, value=1, step=1,
                    key=f"sq_{i}",
                )
                product = st.radio(
                    "Order type",
                    ["Intraday (MIS) — exit by 3:20 PM", "Delivery (CNC) — hold overnight"],
                    key=f"sp_{i}",
                )
                if st.button("✅ Confirm Order", key=f"sc_{i}", type="primary",
                             use_container_width=True):
                    prod_code = "MIS" if "Intraday" in product else "CNC"
                    result = connector.market_order(
                        s["symbol"], s["direction"], int(qty), "CASH", prod_code
                    )
                    if result["status"] == "SUCCESS":
                        db.open_trade(
                            run_id=0, strategy="SIGNAL",
                            symbol=s["symbol"], side=s["direction"],
                            qty=int(qty), entry_px=s["price"],
                            paper=result["mock"],
                        )
                        if result["mock"]:
                            st.success(
                                f"📝 Paper trade logged: {s['direction']} "
                                f"{qty}× {s['symbol']} @ ₹{s['price']:,.2f}"
                            )
                        else:
                            st.success(
                                f"✅ Order placed on Groww!  "
                                f"ID: {result['order_id']}"
                            )
                    else:
                        st.error(f"❌ Order failed: {result.get('error', 'Unknown')}")


# ── Options signals ────────────────────────────────────────────────────────────
st.divider()
st.subheader("📊 Options Signals  ·  NIFTY & BANK NIFTY")

if not opt_sigs:
    st.info(
        "Options chain data is unavailable right now.  "
        "NSE's API is typically accessible during market hours (9:15 AM – 3:30 PM IST)."
    )
else:
    for j, o in enumerate(opt_sigs):
        is_buy = o["color"] == "BUY"
        is_neu = o["color"] == "NEUTRAL"
        css    = "sig-buy" if is_buy else ("sig-sell" if not is_neu else "sig-neu")
        emoji  = "🟢" if is_buy else ("🔴" if not is_neu else "🟡")
        t_css  = "tag-buy" if is_buy else ("tag-sell" if not is_neu else "tag-neu")

        st.markdown(
            f"""
            <div class='sig-card {css}'>
              <div style='display:flex;justify-content:space-between;
                          align-items:flex-start;gap:12px'>
                <div>
                  <span style='font-size:1.2rem;font-weight:800'>
                    {emoji} {o['direction']}
                    &nbsp;&mdash;&nbsp;
                    <span style='font-family:monospace'>{o['symbol']}</span>
                    &nbsp;{o['strike']:,} {o['opt_type']}
                  </span>
                  <div style='color:#475569;font-size:.84rem;margin-top:5px'>
                    {o['reason']}
                  </div>
                </div>
                <span class='tag {t_css}'>{o['color']}</span>
              </div>
              <div class='stat-row'>
                <div class='stat-col'>
                  <span class='stat-lbl'>Index Spot</span>
                  <span class='stat-val'>₹{o['spot']:,.0f}</span>
                </div>
                <div class='stat-col'>
                  <span class='stat-lbl'>Suggested Strike</span>
                  <span class='stat-val'>{o['strike']:,} {o['opt_type']}</span>
                </div>
                <div class='stat-col'>
                  <span class='stat-lbl'>PCR</span>
                  <span class='stat-val'>{o['pcr']:.2f}</span>
                </div>
                <div class='stat-col'>
                  <span class='stat-lbl'>Max Pain</span>
                  <span class='stat-val'>{o['max_pain']:,}</span>
                </div>
                <div class='stat-col'>
                  <span class='stat-lbl'>ATM Strike</span>
                  <span class='stat-val'>{o['atm']:,}</span>
                </div>
              </div>
            </div>
            """,
            unsafe_allow_html=True,
        )

        # ── Place order button for this options signal ─────────────────────
        opt_btn_col, _ = st.columns([1, 5])
        with opt_btn_col:
            opt_label = f"BUY CE" if o["color"] == "BUY" else (
                        f"BUY PE" if o["color"] == "SELL" else "Straddle")
            with st.popover(
                f"🛒 {opt_label} — {o['symbol']} {o['strike']:,} {o['opt_type']}",
                use_container_width=True,
            ):
                is_live = connector.is_connected
                st.markdown(
                    f"**{o['direction']}** · "
                    f"{o['symbol']} {o['strike']:,} {o['opt_type']}"
                )
                st.caption(
                    f"Spot: ₹{o['spot']:,.0f}  ·  ATM: {o['atm']:,}  ·  PCR: {o['pcr']:.2f}"
                )
                st.warning(
                    "⚠️ Options premiums change every second. "
                    "Verify the current LTP on Groww before confirming."
                )
                if is_live:
                    st.success("🟢 Connected — will place a REAL order")
                else:
                    st.info("🔵 Not connected — paper trade only")
                lots = st.number_input(
                    "Lots", min_value=1, value=1, step=1,
                    help="1 lot = 75 for NIFTY · 30 for BANKNIFTY",
                    key=f"oq_{j}",
                )
                lot_size = 75 if o["symbol"] == "NIFTY" else 30
                qty_shares = int(lots) * lot_size
                st.caption(f"= {qty_shares} shares total")
                fno_sym = f"{o['symbol']}{o['strike']}{o['opt_type']}"
                if st.button("✅ Confirm Options Order", key=f"oc_{j}",
                             type="primary", use_container_width=True):
                    result = connector.market_order(
                        fno_sym, "BUY", qty_shares, "FNO", "NRML"
                    )
                    if result["status"] == "SUCCESS":
                        db.open_trade(
                            run_id=0, strategy="OPTIONS_SIGNAL",
                            symbol=fno_sym, side="BUY",
                            qty=qty_shares, entry_px=0.0,
                            paper=result["mock"],
                        )
                        if result["mock"]:
                            st.success(f"📝 Paper trade logged: {fno_sym} × {qty_shares}")
                        else:
                            st.success(f"✅ Options order placed! ID: {result['order_id']}")
                    else:
                        st.error(f"❌ Order failed: {result.get('error', 'Unknown')}")


# ── Scan footer ────────────────────────────────────────────────────────────────
st.divider()
fc1, fc2 = st.columns([5, 1])

last     = screener.last_scan
n_stocks = len(screener.get_results())

if last:
    fc1.caption(
        f"Last scan: **{last.strftime('%d %b %Y %H:%M:%S')}** · "
        f"{n_stocks} stocks analysed · next auto-scan in ~15 min"
    )
else:
    fc1.caption("No scan run yet.")

if fc2.button("🔄 Scan Now", use_container_width=True):
    with st.spinner("Scanning stocks ..."):
        screener.scan()
    st.rerun()


# ── Auto-refresh every 30 s ────────────────────────────────────────────────────
# Page is fully rendered above; browser shows all content.
# The 30-second sleep keeps the session alive and then reruns the script.
time.sleep(30)
st.rerun()
