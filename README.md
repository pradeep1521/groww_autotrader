# Groww AutoTrader

A real-money automated trading system built on the Groww API. Runs 6 concurrent strategies, screens the NSE universe in the background, manages risk per-trade, and exposes everything through a live Streamlit dashboard.

---

## What it does

You launch one Streamlit app. The bot runs in a background thread. Every 5 seconds it evaluates each active strategy, fires orders through the Groww broker API (or paper-trades if you're not connected), and updates the dashboard in real time.

---

## Architecture

```
app.py                  ← Streamlit dashboard (auto-refreshes every 5s)
pages/
  1_Strategies.py       ← Add/configure strategy runs
  2_History.py          ← Closed trade log
  3_Broker_Connect.py   ← Groww login (JWT / TOTP / API key)
  4_Charts.py           ← Candlestick + indicators + one-click trade setup

engine/
  bot.py                ← AutoTrader: thread loop, dispatches all 6 strategies
  indicators.py         ← RSI, EMA, MACD, BB, ATR, ADX, VWAP, VolumeRatio
  screener.py           ← Nifty50 + NiftyNext50 + F&O universe scanner
  options_chain.py      ← NSE live options chain (PCR, MaxPain, OI buildup)
  risk_guard.py         ← Position sizing, SL/target, VIX-adjusted multiples
  pricer.py             ← Black-Scholes options pricer

data/
  feed.py               ← Thread-safe price feed (yfinance, cached)
  db.py                 ← SQLite trade journal (trades.db)

broker/
  groww.py              ← GrowwAPI wrapper with paper-trade fallback
```

---

## Strategies

| Strategy | Signal source | Instruments |
|----------|---------------|-------------|
| **Options Chain** | NSE live chain — PCR / MaxPain / OI buildup | NIFTY / BANKNIFTY options |
| **MTF** | Multi-timeframe EMA + RSI mean-reversion | Nifty reversion picks |
| **Intraday** | VWAP + momentum | Top intraday stocks from screener |
| **Breakout** | Bollinger squeeze + 20-bar high + volume surge | Breakout candidates |
| **Bounce** | RSI oversold + near BB lower band | Oversold bounce candidates |
| **Bulk Order** | Institutional volume spike (ratio ≥ 2.5×) | F&O universe |

---

## Risk management

Every trade goes through `RiskGuard` before execution:

- **Stop-loss**: ATR × multiplier, where multiplier = `1.0 + VIX/10` clamped to [1.5 – 4.0]
- **Target**: always 1.5× R (risk-reward is never less than 1:1.5)
- **Position size**: based on 1% risk-per-trade on ₹5 lakh default capital
- **Max open trades**: 6 concurrent runs
- **Max portfolio exposure**: 80% of capital across all open positions
- **Daily loss kill-switch**: bot auto-stops when daily P&L hits the configured limit
- **VIX kill-switch**: configurable threshold (default 25); when India VIX spikes above it, new entries are blocked — existing positions continue to exit normally

---

## Dashboard features

- **6 KPI tiles**: Bot status · Active runs · Session P&L · **Unrealised P&L** (live from open legs) · Daily P&L · Loss limit %
- **Bot controls**: Start/Stop · Emergency Stop (square off all) · Clear finished runs
- **VIX kill-switch**: toggle + threshold — shows live warning when entries are paused
- **Auto-screener toggle**: bot automatically adds top-momentum stocks as paper runs
- **Market snapshot**: NIFTY / BANKNIFTY / VIX prices + live PCR + MaxPain
- **Screener snapshot**: regime pill (TRENDING / VOLATILE / NORMAL) + top momentum/reversion picks
- **Strategy run cards**: per-run state, P&L, open legs with live LTP and unrealised P&L, expandable log
- **Completed runs bar chart**: visual P&L summary

---

## Screener

Scans ~150 stocks (Nifty50 + NiftyNext50 + F&O extras) and scores each one:

| Scanner | Logic |
|---------|-------|
| `scan()` | Momentum + reversion composite score on 90-day daily OHLCV |
| `scan_breakout()` | BB squeeze → 20-bar high breakout → volume surge |
| `scan_bounce()` | RSI < 35 + price near lower BB band |
| `scan_bulk()` | 5-min volume ratio ≥ 2.5× 20-bar average |

Regime detection reads live VIX and returns `TRENDING` (VIX < 14), `VOLATILE` (VIX > 22), or `NORMAL`.

---

## Data & broker

| Component | Detail |
|-----------|--------|
| **Price feed** | yfinance with 5-day/1-day history fetch; thread-safe in-memory cache; `^NSEI`, `^NSEBANK`, `^INDIAVIX`, etc. |
| **Options chain** | NSE unofficial API (`nseindia.com`) with session cookie auto-refresh every 5 min; chain data cached 60s |
| **Trade journal** | SQLite (`trades.db`); schema: symbol, side, qty, entry/exit price, P&L, paper flag, timestamps |
| **Broker** | GrowwAPI — JWT / TOTP / API-key auth; falls back to paper mode when not connected |

---

## Running it

```bash
# Install dependencies
pip install -r requirements.txt

# Start the app
streamlit run app.py --server.port 8503
```

Set `GROWW_TOTP_SECRET` (or `GROWW_API_KEY` + `GROWW_SECRET`) in your environment for live trading. Without it the app starts in paper mode.

---

## Tests

```bash
python3 -m pytest tests/ -v
```

131 tests, all passing, ~1.7s. No real network calls — NSE API and yfinance are fully mocked.

| File | Tests |
|------|-------|
| `tests/test_indicators.py` | 45 — RSI, EMA, SMA, VWAP, BB, ATR, ADX, MACD, IndicatorEngine |
| `tests/test_risk_guard.py` | 29 — SL/target, position size, VIX multipliers, exposure guard |
| `tests/test_options_chain.py` | 23 — PCR, MaxPain, OI buildup signal modes |
| `tests/test_feed.py` | 14 — cache, symbol mapping, batch refresh |
| `tests/test_db.py` | 20 — open/close trades, P&L, daily summary |
