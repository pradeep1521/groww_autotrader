"""
Tests for data/db.py

Monkeypatches _DB_PATH to a temp file so tests never touch the real trades.db.
"""
import pytest
import sys, os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))


@pytest.fixture
def db(tmp_path, monkeypatch):
    """Fresh db module pointing at a per-test temp SQLite file."""
    import data.db as _db
    monkeypatch.setattr(_db, "_DB_PATH", tmp_path / "test_trades.db")
    _db._init()
    return _db


# ── open_trade ─────────────────────────────────────────────────────────────────

class TestOpenTrade:
    def test_returns_int_id(self, db):
        tid = db.open_trade("run1", "Breakout", "RELIANCE", "BUY", 10, 2500.0)
        assert isinstance(tid, int) and tid > 0

    def test_auto_increments_ids(self, db):
        t1 = db.open_trade("r1", "Breakout", "TCS",  "BUY", 5, 3500.0)
        t2 = db.open_trade("r1", "Bounce",   "TCS",  "BUY", 5, 3510.0)
        assert t2 > t1

    def test_paper_default_true(self, db):
        db.open_trade("r1", "MTF", "INFY", "BUY", 3, 1500.0)
        trades = db.get_trades()
        assert trades[0]["paper"] == 1

    def test_live_trade_flag(self, db):
        db.open_trade("r1", "MTF", "INFY", "BUY", 3, 1500.0, paper=False)
        trades = db.get_trades()
        assert trades[0]["paper"] == 0

    def test_new_trade_is_open_status(self, db):
        db.open_trade("r1", "MTF", "SBIN", "BUY", 20, 750.0)
        trades = db.get_trades()
        assert trades[0]["status"] == "OPEN"


# ── close_trade ────────────────────────────────────────────────────────────────

class TestCloseTrade:
    def test_close_long_trade_profit(self, db):
        tid = db.open_trade("r1", "Breakout", "RELIANCE", "BUY", 10, 2500.0)
        db.close_trade(tid, exit_px=2600.0)
        trades = db.get_trades()
        row = next(t for t in trades if t["id"] == tid)
        assert row["status"] == "CLOSED"
        assert abs(row["pnl"] - 1000.0) < 0.1   # (2600-2500)*10

    def test_close_long_trade_loss(self, db):
        tid = db.open_trade("r1", "Breakout", "TCS", "BUY", 5, 3500.0)
        db.close_trade(tid, exit_px=3400.0)
        trades = db.get_trades()
        row = next(t for t in trades if t["id"] == tid)
        assert row["pnl"] < 0
        assert abs(row["pnl"] - (-500.0)) < 0.1

    def test_close_short_trade_profit(self, db):
        tid = db.open_trade("r1", "Breakout", "HDFCBANK", "SELL", 8, 1600.0)
        db.close_trade(tid, exit_px=1550.0)
        trades = db.get_trades()
        row = next(t for t in trades if t["id"] == tid)
        assert row["pnl"] > 0
        assert abs(row["pnl"] - 400.0) < 0.1     # (1600-1550)*8

    def test_close_sets_exit_px(self, db):
        tid = db.open_trade("r1", "Bounce", "WIPRO", "BUY", 2, 500.0)
        db.close_trade(tid, exit_px=520.0)
        trades = db.get_trades()
        row = next(t for t in trades if t["id"] == tid)
        assert abs(row["exit_px"] - 520.0) < 0.01

    def test_close_nonexistent_trade_is_noop(self, db):
        db.close_trade(999999, exit_px=100.0)   # must not raise


# ── get_trades ─────────────────────────────────────────────────────────────────

class TestGetTrades:
    def test_empty_at_start(self, db):
        assert db.get_trades() == []

    def test_returns_all_trades(self, db):
        db.open_trade("r1", "Breakout", "A", "BUY", 1, 100.0)
        db.open_trade("r1", "Bounce",   "B", "BUY", 1, 200.0)
        trades = db.get_trades()
        assert len(trades) == 2

    def test_ordered_newest_first(self, db):
        t1 = db.open_trade("r1", "MTF", "X", "BUY", 1, 100.0)
        t2 = db.open_trade("r1", "MTF", "Y", "BUY", 1, 200.0)
        trades = db.get_trades()
        assert trades[0]["id"] == t2
        assert trades[1]["id"] == t1

    def test_limit_respected(self, db):
        for i in range(10):
            db.open_trade(f"r{i}", "MTF", "Z", "BUY", 1, 100.0)
        trades = db.get_trades(limit=5)
        assert len(trades) == 5


# ── get_open_trades ────────────────────────────────────────────────────────────

class TestGetOpenTrades:
    def test_empty_at_start(self, db):
        assert db.get_open_trades() == []

    def test_only_open_trades_returned(self, db):
        t1 = db.open_trade("r1", "MTF", "X", "BUY", 1, 100.0)
        t2 = db.open_trade("r1", "MTF", "Y", "BUY", 1, 200.0)
        db.close_trade(t1, exit_px=110.0)
        open_trades = db.get_open_trades()
        assert len(open_trades) == 1
        assert open_trades[0]["id"] == t2

    def test_closed_trade_not_in_open(self, db):
        tid = db.open_trade("r1", "Breakout", "Z", "BUY", 1, 300.0)
        db.close_trade(tid, exit_px=310.0)
        assert db.get_open_trades() == []


# ── daily_pnl ─────────────────────────────────────────────────────────────────

class TestDailyPnl:
    def test_zero_when_no_trades(self, db):
        assert db.daily_pnl() == 0.0

    def test_includes_closed_today(self, db):
        t1 = db.open_trade("r1", "MTF", "A", "BUY", 10, 1000.0)
        t2 = db.open_trade("r1", "MTF", "B", "BUY",  5, 2000.0)
        db.close_trade(t1, exit_px=1100.0)   # +1000
        db.close_trade(t2, exit_px=1900.0)   # -500
        pnl = db.daily_pnl()
        assert abs(pnl - 500.0) < 1.0

    def test_ignores_open_trades(self, db):
        db.open_trade("r1", "MTF", "A", "BUY", 10, 1000.0)   # not closed
        assert db.daily_pnl() == 0.0
