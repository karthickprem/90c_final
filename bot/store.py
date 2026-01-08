"""
SQLite storage for logging and analytics.
Persists market data, signals, orders, positions, and results.
"""

import sqlite3
import logging
import json
from typing import List, Dict, Optional, Any
from datetime import datetime, date
from dataclasses import dataclass, asdict
from pathlib import Path
from contextlib import contextmanager

logger = logging.getLogger(__name__)


class Store:
    """SQLite storage for bot data."""
    
    def __init__(self, db_path: str = "bot_data.db"):
        self.db_path = db_path
        self._init_db()
    
    def _init_db(self):
        """Initialize database schema."""
        with self._connection() as conn:
            conn.executescript("""
                -- Market data snapshots
                CREATE TABLE IF NOT EXISTS markets (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    target_date TEXT NOT NULL,
                    token_id TEXT NOT NULL,
                    tmin_f REAL NOT NULL,
                    tmax_f REAL NOT NULL,
                    best_bid REAL,
                    best_ask REAL,
                    depth_ask_50 REAL,
                    ts TEXT NOT NULL,
                    UNIQUE(target_date, token_id, ts)
                );
                
                -- Trading signals (opportunities detected)
                CREATE TABLE IF NOT EXISTS signals (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    target_date TEXT NOT NULL,
                    interval_id TEXT NOT NULL,
                    interval_tmin REAL NOT NULL,
                    interval_tmax REAL NOT NULL,
                    implied_cost REAL NOT NULL,
                    p_model REAL NOT NULL,
                    edge REAL NOT NULL,
                    forecast_mu REAL,
                    forecast_sigma REAL,
                    chosen INTEGER DEFAULT 0,
                    reason TEXT,
                    ts TEXT NOT NULL
                );
                
                -- Orders placed
                CREATE TABLE IF NOT EXISTS orders (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    order_id TEXT UNIQUE,
                    position_id TEXT,
                    token_id TEXT NOT NULL,
                    side TEXT NOT NULL,
                    shares REAL NOT NULL,
                    limit_price REAL NOT NULL,
                    status TEXT NOT NULL,
                    filled_shares REAL DEFAULT 0,
                    avg_fill_price REAL,
                    ts_created TEXT NOT NULL,
                    ts_updated TEXT
                );
                
                -- Positions (aggregated from orders)
                CREATE TABLE IF NOT EXISTS positions (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    position_id TEXT UNIQUE NOT NULL,
                    target_date TEXT NOT NULL,
                    interval_tmin REAL NOT NULL,
                    interval_tmax REAL NOT NULL,
                    total_cost REAL NOT NULL,
                    payout_if_hit REAL NOT NULL,
                    max_loss REAL NOT NULL,
                    shares_per_leg REAL NOT NULL,
                    num_legs INTEGER NOT NULL,
                    token_ids TEXT NOT NULL,  -- JSON array
                    status TEXT DEFAULT 'open',
                    realized_pnl REAL,
                    ts_opened TEXT NOT NULL,
                    ts_closed TEXT
                );
                
                -- Settlement results
                CREATE TABLE IF NOT EXISTS results (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    target_date TEXT NOT NULL,
                    position_id TEXT,
                    observed_high_f REAL,
                    winning_bucket_tmin REAL,
                    winning_bucket_tmax REAL,
                    interval_hit INTEGER,  -- 1 if observed temp was in our interval
                    pnl REAL,
                    ts TEXT NOT NULL
                );
                
                -- Forecasts (for model calibration)
                CREATE TABLE IF NOT EXISTS forecasts (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    target_date TEXT NOT NULL,
                    location TEXT NOT NULL,
                    forecast_mu REAL NOT NULL,
                    forecast_sigma REAL NOT NULL,
                    source TEXT,
                    ts_fetched TEXT NOT NULL
                );
                
                -- Create indexes
                CREATE INDEX IF NOT EXISTS idx_markets_date ON markets(target_date);
                CREATE INDEX IF NOT EXISTS idx_signals_date ON signals(target_date);
                CREATE INDEX IF NOT EXISTS idx_orders_position ON orders(position_id);
                CREATE INDEX IF NOT EXISTS idx_positions_date ON positions(target_date);
                CREATE INDEX IF NOT EXISTS idx_results_date ON results(target_date);
            """)
    
    @contextmanager
    def _connection(self):
        """Context manager for database connections."""
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        try:
            yield conn
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()
    
    def log_market(self, target_date: date, token_id: str,
                   tmin_f: float, tmax_f: float,
                   best_bid: Optional[float], best_ask: Optional[float],
                   depth_ask_50: Optional[float] = None):
        """Log a market data snapshot."""
        ts = datetime.now().isoformat()
        with self._connection() as conn:
            conn.execute("""
                INSERT OR REPLACE INTO markets 
                (target_date, token_id, tmin_f, tmax_f, best_bid, best_ask, depth_ask_50, ts)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """, (target_date.isoformat(), token_id, tmin_f, tmax_f, 
                  best_bid, best_ask, depth_ask_50, ts))
    
    def log_signal(self, target_date: date, interval_id: str,
                   interval_tmin: float, interval_tmax: float,
                   implied_cost: float, p_model: float, edge: float,
                   forecast_mu: float = None, forecast_sigma: float = None,
                   chosen: bool = False, reason: str = None):
        """Log a trading signal."""
        ts = datetime.now().isoformat()
        with self._connection() as conn:
            conn.execute("""
                INSERT INTO signals 
                (target_date, interval_id, interval_tmin, interval_tmax,
                 implied_cost, p_model, edge, forecast_mu, forecast_sigma, chosen, reason, ts)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (target_date.isoformat(), interval_id, interval_tmin, interval_tmax,
                  implied_cost, p_model, edge, forecast_mu, forecast_sigma,
                  1 if chosen else 0, reason, ts))
    
    def log_order(self, order_id: str, position_id: str, token_id: str,
                  side: str, shares: float, limit_price: float, status: str,
                  filled_shares: float = 0, avg_fill_price: float = None):
        """Log an order."""
        ts = datetime.now().isoformat()
        with self._connection() as conn:
            conn.execute("""
                INSERT OR REPLACE INTO orders
                (order_id, position_id, token_id, side, shares, limit_price,
                 status, filled_shares, avg_fill_price, ts_created, ts_updated)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (order_id, position_id, token_id, side, shares, limit_price,
                  status, filled_shares, avg_fill_price, ts, ts))
    
    def update_order(self, order_id: str, status: str, 
                     filled_shares: float = None, avg_fill_price: float = None):
        """Update an existing order."""
        ts = datetime.now().isoformat()
        with self._connection() as conn:
            updates = ["status = ?", "ts_updated = ?"]
            params = [status, ts]
            
            if filled_shares is not None:
                updates.append("filled_shares = ?")
                params.append(filled_shares)
            
            if avg_fill_price is not None:
                updates.append("avg_fill_price = ?")
                params.append(avg_fill_price)
            
            params.append(order_id)
            conn.execute(f"""
                UPDATE orders SET {', '.join(updates)}
                WHERE order_id = ?
            """, params)
    
    def log_position(self, position_id: str, target_date: date,
                     interval_tmin: float, interval_tmax: float,
                     total_cost: float, payout_if_hit: float,
                     shares_per_leg: float, token_ids: List[str]):
        """Log a new position."""
        ts = datetime.now().isoformat()
        with self._connection() as conn:
            conn.execute("""
                INSERT INTO positions
                (position_id, target_date, interval_tmin, interval_tmax,
                 total_cost, payout_if_hit, max_loss, shares_per_leg,
                 num_legs, token_ids, status, ts_opened)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'open', ?)
            """, (position_id, target_date.isoformat(), interval_tmin, interval_tmax,
                  total_cost, payout_if_hit, total_cost, shares_per_leg,
                  len(token_ids), json.dumps(token_ids), ts))
    
    def close_position(self, position_id: str, realized_pnl: float):
        """Close a position with realized P&L."""
        ts = datetime.now().isoformat()
        with self._connection() as conn:
            conn.execute("""
                UPDATE positions 
                SET status = 'closed', realized_pnl = ?, ts_closed = ?
                WHERE position_id = ?
            """, (realized_pnl, ts, position_id))
    
    def log_result(self, target_date: date, position_id: str = None,
                   observed_high_f: float = None, 
                   winning_bucket: tuple = None,
                   interval_hit: bool = None, pnl: float = None):
        """Log a settlement result."""
        ts = datetime.now().isoformat()
        win_tmin = winning_bucket[0] if winning_bucket else None
        win_tmax = winning_bucket[1] if winning_bucket else None
        with self._connection() as conn:
            conn.execute("""
                INSERT INTO results
                (target_date, position_id, observed_high_f, 
                 winning_bucket_tmin, winning_bucket_tmax, interval_hit, pnl, ts)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """, (target_date.isoformat(), position_id, observed_high_f,
                  win_tmin, win_tmax, 1 if interval_hit else 0, pnl, ts))
    
    def log_forecast(self, target_date: date, location: str,
                     forecast_mu: float, forecast_sigma: float, source: str = None):
        """Log a forecast for later calibration."""
        ts = datetime.now().isoformat()
        with self._connection() as conn:
            conn.execute("""
                INSERT INTO forecasts
                (target_date, location, forecast_mu, forecast_sigma, source, ts_fetched)
                VALUES (?, ?, ?, ?, ?, ?)
            """, (target_date.isoformat(), location, forecast_mu, forecast_sigma, source, ts))
    
    def get_open_positions(self) -> List[Dict]:
        """Get all open positions."""
        with self._connection() as conn:
            rows = conn.execute("""
                SELECT * FROM positions WHERE status = 'open'
            """).fetchall()
            return [dict(row) for row in rows]
    
    def get_positions_for_date(self, target_date: date) -> List[Dict]:
        """Get all positions for a target date."""
        with self._connection() as conn:
            rows = conn.execute("""
                SELECT * FROM positions WHERE target_date = ?
            """, (target_date.isoformat(),)).fetchall()
            return [dict(row) for row in rows]
    
    def get_signals_for_date(self, target_date: date) -> List[Dict]:
        """Get all signals for a target date."""
        with self._connection() as conn:
            rows = conn.execute("""
                SELECT * FROM signals WHERE target_date = ? ORDER BY edge DESC
            """, (target_date.isoformat(),)).fetchall()
            return [dict(row) for row in rows]
    
    def get_results_summary(self, days: int = 30) -> Dict:
        """Get summary of results over past N days."""
        with self._connection() as conn:
            row = conn.execute("""
                SELECT 
                    COUNT(*) as total_trades,
                    SUM(CASE WHEN interval_hit = 1 THEN 1 ELSE 0 END) as wins,
                    SUM(CASE WHEN interval_hit = 0 THEN 1 ELSE 0 END) as losses,
                    SUM(pnl) as total_pnl,
                    AVG(pnl) as avg_pnl
                FROM results
                WHERE ts >= date('now', ?)
            """, (f'-{days} days',)).fetchone()
            
            return dict(row) if row else {}
    
    def get_forecast_accuracy(self, location: str, days: int = 30) -> List[Dict]:
        """
        Compare forecasts to actual results for model calibration.
        Returns list of (forecast_mu, forecast_sigma, observed_high) tuples.
        """
        with self._connection() as conn:
            rows = conn.execute("""
                SELECT f.target_date, f.forecast_mu, f.forecast_sigma, 
                       r.observed_high_f
                FROM forecasts f
                JOIN results r ON f.target_date = r.target_date
                WHERE f.location = ?
                  AND f.ts_fetched >= date('now', ?)
                  AND r.observed_high_f IS NOT NULL
                ORDER BY f.target_date
            """, (location, f'-{days} days')).fetchall()
            return [dict(row) for row in rows]


if __name__ == "__main__":
    # Test store
    import os
    
    test_db = "test_bot_data.db"
    if os.path.exists(test_db):
        os.remove(test_db)
    
    store = Store(test_db)
    
    # Test logging
    today = date.today()
    
    store.log_market(today, "token123", 50.0, 51.0, 0.10, 0.12, 100)
    store.log_signal(today, "interval_50_53", 50.0, 53.0, 0.35, 0.42, 0.07,
                     forecast_mu=52.0, forecast_sigma=2.0, chosen=True)
    store.log_position("pos_001", today, 50.0, 53.0, 3.50, 10.0, 10.0, 
                       ["token1", "token2", "token3"])
    store.log_order("order_001", "pos_001", "token1", "BUY", 10, 0.12, "FILLED", 10, 0.115)
    store.log_forecast(today, "London", 52.0, 2.0, "open_meteo")
    
    print("Open positions:", store.get_open_positions())
    print("Signals for today:", store.get_signals_for_date(today))
    
    # Cleanup
    os.remove(test_db)
    print("\nStore tests passed!")

