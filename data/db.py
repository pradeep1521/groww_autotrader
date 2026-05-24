"""
Trade Journal — SQLite-backed log of every order placed (paper or live).
"""

import sqlite3
import threading
from datetime import datetime
from pathlib import Path

_DB_PATH = Path(__file__).parent.parent / "trades.db"
_lock    = threading.Lock()


def _conn() -> sqlite3.Connection:
    con = sqlite3.connect(str(_DB_PATH), check_same_thread=False)
    con.row_factory = sqlite3.Row
    return con


def _init() -> None:
    with _conn() as con:
        con.execute("""
        CREATE TABLE IF NOT EXISTS trades (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            run_id      TEXT,
            strategy    TEXT,
            symbol      TEXT,
            side        TEXT,
            qty         INTEGER,
            entry_px    REAL,
            exit_px     REAL,
            pnl         REAL,
            status      TEXT DEFAULT 'OPEN',
            paper       INTEGER DEFAULT 1,
            note        TEXT,
            opened_at   TEXT,
            closed_at   TEXT
        )""")
        con.commit()


_init()


# ── Write ──────────────────────────────────────────────────────────────────────

def open_trade(
    run_id:   str,
    strategy: str,
    symbol:   str,
    side:     str,
    qty:      int,
    entry_px: float,
    paper:    bool = True,
    note:     str  = "",
) -> int:
    with _lock, _conn() as con:
        cur = con.execute(
            """INSERT INTO trades
               (run_id, strategy, symbol, side, qty, entry_px, paper, note, opened_at)
               VALUES (?,?,?,?,?,?,?,?,?)""",
            (run_id, strategy, symbol, side, int(qty), float(entry_px),
             int(paper), note, datetime.now().isoformat()),
        )
        con.commit()
        return cur.lastrowid


def close_trade(trade_id: int, exit_px: float) -> None:
    with _lock, _conn() as con:
        con.execute("""SELECT side, qty, entry_px FROM trades WHERE id=?""", (trade_id,))
        row = con.execute("SELECT side, qty, entry_px FROM trades WHERE id=?", (trade_id,)).fetchone()
        if not row:
            return
        pnl = (row["entry_px"] - exit_px) * row["qty"] if row["side"] == "SELL" \
              else (exit_px - row["entry_px"]) * row["qty"]
        con.execute(
            "UPDATE trades SET exit_px=?, pnl=?, status='CLOSED', closed_at=? WHERE id=?",
            (float(exit_px), float(pnl), datetime.now().isoformat(), trade_id),
        )
        con.commit()


# ── Read ───────────────────────────────────────────────────────────────────────

def get_trades(limit: int = 500) -> list[dict]:
    with _lock, _conn() as con:
        rows = con.execute(
            "SELECT * FROM trades ORDER BY id DESC LIMIT ?", (limit,)
        ).fetchall()
    return [dict(r) for r in rows]


def get_open_trades() -> list[dict]:
    with _lock, _conn() as con:
        rows = con.execute(
            "SELECT * FROM trades WHERE status='OPEN' ORDER BY id DESC"
        ).fetchall()
    return [dict(r) for r in rows]


def daily_pnl() -> float:
    today = datetime.now().strftime("%Y-%m-%d")
    with _lock, _conn() as con:
        row = con.execute(
            "SELECT COALESCE(SUM(pnl),0) FROM trades WHERE status='CLOSED' AND closed_at LIKE ?",
            (today + "%",),
        ).fetchone()
    return float(row[0]) if row else 0.0
