"""SQLite database for trade logging and performance tracking."""

import logging
import sqlite3
import threading
from datetime import datetime, timezone
from pathlib import Path

logger = logging.getLogger(__name__)


class Database:
    """SQLite-backed trade journal. Thread-safe."""

    def __init__(self, db_path: str = "data/hft_london_1pct.db"):
        self._db_path = db_path
        Path(db_path).parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._conn = sqlite3.connect(db_path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._create_tables()

    def _execute(self, query: str, params: tuple = ()):
        with self._lock:
            self._conn.execute(query, params)
            self._conn.commit()

    def _query(self, query: str, params: tuple = ()) -> list:
        with self._lock:
            return self._conn.execute(query, params).fetchall()

    def _query_one(self, query: str, params: tuple = ()):
        with self._lock:
            return self._conn.execute(query, params).fetchone()

    def _create_tables(self):
        with self._lock:
            self._conn.cursor().executescript("""
                CREATE TABLE IF NOT EXISTS trades (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    ticket INTEGER UNIQUE,
                    symbol TEXT NOT NULL,
                    direction TEXT NOT NULL,
                    lots REAL NOT NULL,
                    entry_price REAL NOT NULL,
                    exit_price REAL,
                    sl REAL,
                    tp REAL,
                    profit_usd REAL,
                    profit_pct REAL,
                    commission REAL DEFAULT 0,
                    swap REAL DEFAULT 0,
                    strategy TEXT,
                    entry_time TEXT NOT NULL,
                    exit_time TEXT,
                    equity_at_entry REAL,
                    equity_at_exit REAL,
                    status TEXT DEFAULT 'open',
                    comment TEXT
                );

                CREATE TABLE IF NOT EXISTS daily_summary (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    date TEXT UNIQUE NOT NULL,
                    start_equity REAL NOT NULL,
                    end_equity REAL NOT NULL,
                    pnl_usd REAL NOT NULL,
                    pnl_pct REAL NOT NULL,
                    trades_count INTEGER DEFAULT 0,
                    wins INTEGER DEFAULT 0,
                    losses INTEGER DEFAULT 0,
                    max_drawdown_pct REAL DEFAULT 0,
                    circuit_breaker_hit INTEGER DEFAULT 0
                );

                CREATE TABLE IF NOT EXISTS equity_snapshots (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    timestamp TEXT NOT NULL,
                    equity REAL NOT NULL,
                    balance REAL NOT NULL,
                    unrealized_pnl REAL DEFAULT 0,
                    open_positions INTEGER DEFAULT 0
                );
            """)
            self._conn.commit()
        logger.info("Database initialized: %s", self._db_path)

    # -- Trade operations ------------------------------------------------------

    def log_trade_open(self, ticket, symbol, direction, lots, entry_price, sl, tp,
                       strategy, equity_at_entry, comment=""):
        self._execute(
            """INSERT OR REPLACE INTO trades
               (ticket, symbol, direction, lots, entry_price, sl, tp,
                strategy, entry_time, equity_at_entry, status, comment)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'open', ?)""",
            (ticket, symbol, direction, lots, entry_price, sl, tp,
             strategy, datetime.now(timezone.utc).isoformat(), equity_at_entry, comment),
        )

    def log_trade_close(self, ticket, exit_price, profit_usd, equity_at_exit,
                        commission=0, swap=0):
        row = self._query_one(
            "SELECT equity_at_entry FROM trades WHERE ticket = ?", (ticket,)
        )
        profit_pct = 0.0
        if row and row["equity_at_entry"] and row["equity_at_entry"] > 0:
            profit_pct = (profit_usd / row["equity_at_entry"]) * 100.0

        self._execute(
            """UPDATE trades SET
               exit_price = ?, profit_usd = ?, profit_pct = ?,
               commission = ?, swap = ?, equity_at_exit = ?,
               exit_time = ?, status = 'closed'
               WHERE ticket = ?""",
            (exit_price, profit_usd, round(profit_pct, 4),
             commission, swap, equity_at_exit,
             datetime.now(timezone.utc).isoformat(), ticket),
        )

    # -- Query operations ------------------------------------------------------

    def get_open_trades(self) -> list[dict]:
        rows = self._query("SELECT * FROM trades WHERE status = 'open' ORDER BY entry_time DESC")
        return [dict(r) for r in rows]

    def get_closed_trades(self, limit: int = 50) -> list[dict]:
        rows = self._query(
            "SELECT * FROM trades WHERE status = 'closed' ORDER BY exit_time DESC LIMIT ?",
            (limit,),
        )
        return [dict(r) for r in rows]

    def get_today_trades(self) -> list[dict]:
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        rows = self._query(
            "SELECT * FROM trades WHERE entry_time LIKE ? OR exit_time LIKE ? ORDER BY entry_time DESC",
            (f"{today}%", f"{today}%"),
        )
        return [dict(r) for r in rows]

    # -- Daily summary ---------------------------------------------------------

    def save_daily_summary(self, date, start_equity, end_equity, trades_count,
                           wins, losses, max_drawdown_pct, circuit_breaker_hit):
        pnl_usd = end_equity - start_equity
        pnl_pct = (pnl_usd / start_equity * 100) if start_equity > 0 else 0
        self._execute(
            """INSERT OR REPLACE INTO daily_summary
               (date, start_equity, end_equity, pnl_usd, pnl_pct,
                trades_count, wins, losses, max_drawdown_pct, circuit_breaker_hit)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (date, start_equity, end_equity, round(pnl_usd, 2),
             round(pnl_pct, 4), trades_count, wins, losses,
             round(max_drawdown_pct, 4), int(circuit_breaker_hit)),
        )

    def get_daily_summaries(self, days: int = 30) -> list[dict]:
        rows = self._query("SELECT * FROM daily_summary ORDER BY date DESC LIMIT ?", (days,))
        return [dict(r) for r in rows]

    # -- Equity snapshots ------------------------------------------------------

    def save_equity_snapshot(self, equity, balance, unrealized_pnl, open_positions):
        self._execute(
            "INSERT INTO equity_snapshots (timestamp, equity, balance, unrealized_pnl, open_positions) VALUES (?, ?, ?, ?, ?)",
            (datetime.now(timezone.utc).isoformat(), equity, balance, unrealized_pnl, open_positions),
        )

    def get_equity_curve(self, limit: int = 1000) -> list[dict]:
        rows = self._query("SELECT * FROM equity_snapshots ORDER BY timestamp DESC LIMIT ?", (limit,))
        return [dict(r) for r in reversed(rows)]

    # -- Performance stats -----------------------------------------------------

    def get_performance_stats(self) -> dict:
        rows = self._query("SELECT profit_usd, profit_pct FROM trades WHERE status = 'closed'")
        if not rows:
            return {"total_trades": 0, "win_rate_pct": 0, "profit_factor": 0,
                    "total_pnl_pct": 0, "avg_win_pct": 0, "avg_loss_pct": 0}

        wins = [r["profit_usd"] for r in rows if r["profit_usd"] > 0]
        losses = [r["profit_usd"] for r in rows if r["profit_usd"] < 0]
        total_pnl_pct = sum(r["profit_pct"] for r in rows if r["profit_pct"] is not None)
        gross_profit = sum(wins) if wins else 0
        gross_loss = abs(sum(losses)) if losses else 0

        return {
            "total_trades": len(rows),
            "wins": len(wins),
            "losses": len(losses),
            "win_rate_pct": round(len(wins) / len(rows) * 100, 1) if rows else 0,
            "profit_factor": round(gross_profit / gross_loss, 2) if gross_loss > 0 else float("inf"),
            "total_pnl_pct": round(total_pnl_pct, 2),
            "avg_win_pct": round(sum(r["profit_pct"] for r in rows if r["profit_usd"] > 0) / len(wins), 2) if wins else 0,
            "avg_loss_pct": round(sum(r["profit_pct"] for r in rows if r["profit_usd"] < 0) / len(losses), 2) if losses else 0,
        }

    def close(self):
        self._conn.close()
