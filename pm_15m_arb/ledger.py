"""
Ledger Module - SQLite Storage for PM 15m Arb Bot

Persists:
- Windows traded
- Tick samples (optional)
- Orders and fills
- Position snapshots
- Summary statistics

Enables analysis of:
- PnL per window
- Edge capture vs theoretical
- Slippage distribution
- Legging frequency
"""

import sqlite3
import logging
import json
from typing import List, Dict, Optional, Any
from datetime import datetime, date
from pathlib import Path
from contextlib import contextmanager

from .config import ArbConfig, load_config

logger = logging.getLogger(__name__)


class Ledger:
    """
    SQLite-based ledger for PM 15m arb bot.
    
    Tables:
    - windows: Trading window summaries
    - orders: Order submissions
    - fills: Order fills
    - positions: Position state snapshots
    - daily_summary: Aggregated daily stats
    - session_summary: Current session stats
    """
    
    def __init__(self, config: ArbConfig = None):
        self.config = config or load_config()
        self.db_path = self.config.db_path
        self._init_db()
        
        # Session tracking
        self.session_id = datetime.now().strftime("%Y%m%d_%H%M%S")
        self._session_windows = 0
        self._session_pnl = 0.0
        self._session_trades = 0
        self._session_legging = 0
    
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
    
    def _init_db(self):
        """Initialize database schema."""
        with self._connection() as conn:
            conn.executescript("""
                -- Trading windows
                CREATE TABLE IF NOT EXISTS windows (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    window_id TEXT NOT NULL,
                    market_id TEXT NOT NULL,
                    session_id TEXT,
                    start_ts TEXT,
                    end_ts TEXT,
                    
                    -- Position at end
                    qty_yes REAL DEFAULT 0,
                    qty_no REAL DEFAULT 0,
                    cost_yes REAL DEFAULT 0,
                    cost_no REAL DEFAULT 0,
                    safe_profit_net REAL DEFAULT 0,
                    
                    -- Metrics
                    trades_count INTEGER DEFAULT 0,
                    pairs_filled INTEGER DEFAULT 0,
                    legging_events INTEGER DEFAULT 0,
                    signals_seen INTEGER DEFAULT 0,
                    signals_taken INTEGER DEFAULT 0,
                    ticks_processed INTEGER DEFAULT 0,
                    
                    -- Edge analysis
                    theoretical_edge REAL,
                    realized_edge REAL,
                    avg_slippage REAL,
                    
                    stop_reason TEXT,
                    created_at TEXT NOT NULL,
                    
                    UNIQUE(window_id, session_id)
                );
                
                -- Orders
                CREATE TABLE IF NOT EXISTS orders (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    order_id TEXT NOT NULL,
                    window_id TEXT NOT NULL,
                    market_id TEXT NOT NULL,
                    side TEXT NOT NULL,
                    token_id TEXT,
                    price REAL NOT NULL,
                    qty REAL NOT NULL,
                    status TEXT NOT NULL,
                    filled_qty REAL DEFAULT 0,
                    fill_price REAL,
                    slippage REAL DEFAULT 0,
                    created_at TEXT NOT NULL,
                    filled_at TEXT
                );
                
                -- Fills
                CREATE TABLE IF NOT EXISTS fills (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    order_id TEXT NOT NULL,
                    window_id TEXT NOT NULL,
                    side TEXT NOT NULL,
                    qty REAL NOT NULL,
                    price REAL NOT NULL,
                    slippage REAL DEFAULT 0,
                    partial INTEGER DEFAULT 0,
                    created_at TEXT NOT NULL
                );
                
                -- Position snapshots
                CREATE TABLE IF NOT EXISTS positions (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    window_id TEXT NOT NULL,
                    seq INTEGER NOT NULL,
                    qty_yes REAL DEFAULT 0,
                    qty_no REAL DEFAULT 0,
                    cost_yes REAL DEFAULT 0,
                    cost_no REAL DEFAULT 0,
                    safe_profit_net REAL DEFAULT 0,
                    ts TEXT NOT NULL
                );
                
                -- Daily summary
                CREATE TABLE IF NOT EXISTS daily_summary (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    date TEXT UNIQUE NOT NULL,
                    windows_count INTEGER DEFAULT 0,
                    total_trades INTEGER DEFAULT 0,
                    pairs_filled INTEGER DEFAULT 0,
                    legging_events INTEGER DEFAULT 0,
                    total_pnl REAL DEFAULT 0,
                    best_window_pnl REAL,
                    worst_window_pnl REAL,
                    avg_edge REAL,
                    avg_slippage REAL,
                    updated_at TEXT
                );
                
                -- Legging events
                CREATE TABLE IF NOT EXISTS legging_events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    window_id TEXT NOT NULL,
                    event_type TEXT NOT NULL,
                    filled_leg TEXT NOT NULL,
                    action TEXT NOT NULL,
                    loss REAL DEFAULT 0,
                    details TEXT,
                    created_at TEXT NOT NULL
                );
                
                -- Indexes
                CREATE INDEX IF NOT EXISTS idx_windows_date ON windows(created_at);
                CREATE INDEX IF NOT EXISTS idx_windows_session ON windows(session_id);
                CREATE INDEX IF NOT EXISTS idx_orders_window ON orders(window_id);
                CREATE INDEX IF NOT EXISTS idx_fills_window ON fills(window_id);
                CREATE INDEX IF NOT EXISTS idx_positions_window ON positions(window_id);
            """)
    
    def log_window(self, market_id: str, window_id: str,
                   position: Dict[str, Any], safe_profit_net: float,
                   theoretical_edge: float = None,
                   signals_seen: int = 0, signals_taken: int = 0,
                   ticks_processed: int = 0, stop_reason: str = ""):
        """Log a completed trading window."""
        ts = datetime.now().isoformat()
        
        # Update session counters
        self._session_windows += 1
        self._session_pnl += safe_profit_net
        self._session_trades += position.get("trades_count", 0)
        self._session_legging += position.get("legging_events", 0)
        
        with self._connection() as conn:
            conn.execute("""
                INSERT OR REPLACE INTO windows (
                    window_id, market_id, session_id,
                    qty_yes, qty_no, cost_yes, cost_no, safe_profit_net,
                    trades_count, pairs_filled, legging_events,
                    signals_seen, signals_taken, ticks_processed,
                    theoretical_edge, stop_reason, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                window_id, market_id, self.session_id,
                position.get("qty_yes", 0),
                position.get("qty_no", 0),
                position.get("cost_yes", 0),
                position.get("cost_no", 0),
                safe_profit_net,
                position.get("trades_count", 0),
                position.get("pairs_filled", 0),
                position.get("legging_events", 0),
                signals_seen, signals_taken, ticks_processed,
                theoretical_edge, stop_reason, ts
            ))
    
    def log_order(self, order_id: str, window_id: str, market_id: str,
                  side: str, token_id: str, price: float, qty: float, status: str):
        """Log an order submission."""
        ts = datetime.now().isoformat()
        
        with self._connection() as conn:
            conn.execute("""
                INSERT INTO orders (
                    order_id, window_id, market_id, side, token_id,
                    price, qty, status, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (order_id, window_id, market_id, side, token_id, price, qty, status, ts))
    
    def log_fill(self, order_id: str, window_id: str, side: str,
                 qty: float, price: float, slippage: float = 0, partial: bool = False):
        """Log an order fill."""
        ts = datetime.now().isoformat()
        
        with self._connection() as conn:
            conn.execute("""
                INSERT INTO fills (
                    order_id, window_id, side, qty, price, slippage, partial, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """, (order_id, window_id, side, qty, price, slippage, 1 if partial else 0, ts))
            
            # Update order status
            conn.execute("""
                UPDATE orders SET status = ?, filled_qty = ?, fill_price = ?, 
                    slippage = ?, filled_at = ?
                WHERE order_id = ?
            """, ("FILLED" if not partial else "PARTIAL", qty, price, slippage, ts, order_id))
    
    def log_position_snapshot(self, window_id: str, seq: int,
                              qty_yes: float, qty_no: float,
                              cost_yes: float, cost_no: float,
                              safe_profit_net: float):
        """Log position state snapshot."""
        ts = datetime.now().isoformat()
        
        with self._connection() as conn:
            conn.execute("""
                INSERT INTO positions (
                    window_id, seq, qty_yes, qty_no, cost_yes, cost_no,
                    safe_profit_net, ts
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """, (window_id, seq, qty_yes, qty_no, cost_yes, cost_no, safe_profit_net, ts))
    
    def log_legging_event(self, window_id: str, event_type: str,
                          filled_leg: str, action: str,
                          loss: float = 0, details: Dict = None):
        """Log a legging event."""
        ts = datetime.now().isoformat()
        
        with self._connection() as conn:
            conn.execute("""
                INSERT INTO legging_events (
                    window_id, event_type, filled_leg, action, loss, details, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
            """, (window_id, event_type, filled_leg, action, loss,
                  json.dumps(details) if details else None, ts))
    
    def update_daily_summary(self):
        """Update daily summary for today."""
        today = date.today().isoformat()
        ts = datetime.now().isoformat()
        
        with self._connection() as conn:
            # Get today's stats
            stats = conn.execute("""
                SELECT 
                    COUNT(*) as windows_count,
                    SUM(trades_count) as total_trades,
                    SUM(pairs_filled) as pairs_filled,
                    SUM(legging_events) as legging_events,
                    SUM(safe_profit_net) as total_pnl,
                    MAX(safe_profit_net) as best_window,
                    MIN(safe_profit_net) as worst_window,
                    AVG(theoretical_edge) as avg_edge,
                    AVG(avg_slippage) as avg_slippage
                FROM windows
                WHERE date(created_at) = ?
            """, (today,)).fetchone()
            
            conn.execute("""
                INSERT INTO daily_summary (
                    date, windows_count, total_trades, pairs_filled,
                    legging_events, total_pnl, best_window_pnl, worst_window_pnl,
                    avg_edge, avg_slippage, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(date) DO UPDATE SET
                    windows_count = excluded.windows_count,
                    total_trades = excluded.total_trades,
                    pairs_filled = excluded.pairs_filled,
                    legging_events = excluded.legging_events,
                    total_pnl = excluded.total_pnl,
                    best_window_pnl = excluded.best_window_pnl,
                    worst_window_pnl = excluded.worst_window_pnl,
                    avg_edge = excluded.avg_edge,
                    avg_slippage = excluded.avg_slippage,
                    updated_at = excluded.updated_at
            """, (
                today,
                stats["windows_count"] or 0,
                stats["total_trades"] or 0,
                stats["pairs_filled"] or 0,
                stats["legging_events"] or 0,
                stats["total_pnl"] or 0,
                stats["best_window"],
                stats["worst_window"],
                stats["avg_edge"],
                stats["avg_slippage"],
                ts
            ))
    
    def update_session_summary(self):
        """Update session summary (called periodically)."""
        self.update_daily_summary()
    
    def get_session_summary(self) -> Dict[str, Any]:
        """Get current session statistics."""
        return {
            "session_id": self.session_id,
            "windows_count": self._session_windows,
            "total_pnl": round(self._session_pnl, 4),
            "total_trades": self._session_trades,
            "total_legging": self._session_legging,
            "avg_pnl_per_window": round(self._session_pnl / max(1, self._session_windows), 4),
        }
    
    def get_stats_summary(self) -> Dict[str, Any]:
        """Get overall statistics summary."""
        with self._connection() as conn:
            # Overall window stats
            overall = conn.execute("""
                SELECT 
                    COUNT(*) as windows_count,
                    SUM(trades_count) as total_trades,
                    SUM(pairs_filled) as successful_pairs,
                    SUM(legging_events) as legging_events,
                    SUM(safe_profit_net) as total_pnl,
                    AVG(safe_profit_net) as avg_pnl_per_window,
                    MAX(safe_profit_net) as best_window_pnl,
                    MIN(safe_profit_net) as worst_window_pnl,
                    AVG(theoretical_edge) as avg_theoretical_edge
                FROM windows
            """).fetchone()
            
            # Calculate realized edge
            total_cost = conn.execute("""
                SELECT SUM(cost_yes + cost_no) as total_cost,
                       SUM(qty_yes + qty_no) / 2 as total_shares
                FROM windows
            """).fetchone()
            
            # Slippage stats from fills
            slippage_stats = conn.execute("""
                SELECT AVG(slippage) as avg_slippage,
                       MAX(slippage) as max_slippage
                FROM fills
            """).fetchone()
            
            avg_pnl = overall["avg_pnl_per_window"] or 0
            total_shares = total_cost["total_shares"] or 0
            avg_realized_edge = avg_pnl / max(0.01, total_shares / max(1, overall["windows_count"] or 1))
            
            # Edge capture ratio
            avg_theoretical = overall["avg_theoretical_edge"] or 0
            edge_capture = avg_realized_edge / max(0.001, avg_theoretical) if avg_theoretical else 0
            
            return {
                "windows_count": overall["windows_count"] or 0,
                "total_trades": overall["total_trades"] or 0,
                "successful_pairs": overall["successful_pairs"] or 0,
                "legging_events": overall["legging_events"] or 0,
                "total_pnl": overall["total_pnl"] or 0,
                "avg_pnl_per_window": avg_pnl,
                "best_window_pnl": overall["best_window_pnl"] or 0,
                "worst_window_pnl": overall["worst_window_pnl"] or 0,
                "avg_theoretical_edge": avg_theoretical,
                "avg_realized_edge": avg_realized_edge,
                "edge_capture_ratio": edge_capture,
                "avg_slippage": slippage_stats["avg_slippage"] or 0,
                "max_slippage": slippage_stats["max_slippage"] or 0,
            }
    
    def get_windows(self, days: int = 7, session_id: str = None) -> List[Dict]:
        """Get window records for analysis."""
        with self._connection() as conn:
            if session_id:
                rows = conn.execute("""
                    SELECT * FROM windows WHERE session_id = ?
                    ORDER BY created_at DESC
                """, (session_id,)).fetchall()
            else:
                rows = conn.execute("""
                    SELECT * FROM windows
                    WHERE created_at >= date('now', ?)
                    ORDER BY created_at DESC
                """, (f'-{days} days',)).fetchall()
            
            return [dict(row) for row in rows]
    
    def get_pnl_distribution(self, days: int = 30) -> List[float]:
        """Get PnL distribution for histogram."""
        with self._connection() as conn:
            rows = conn.execute("""
                SELECT safe_profit_net FROM windows
                WHERE created_at >= date('now', ?)
                ORDER BY safe_profit_net
            """, (f'-{days} days',)).fetchall()
            
            return [row["safe_profit_net"] for row in rows]
    
    def get_legging_summary(self) -> Dict[str, Any]:
        """Get legging event summary."""
        with self._connection() as conn:
            events = conn.execute("""
                SELECT event_type, action, COUNT(*) as count, SUM(loss) as total_loss
                FROM legging_events
                GROUP BY event_type, action
            """).fetchall()
            
            return {
                "events": [dict(e) for e in events],
                "total_count": sum(e["count"] for e in events),
                "total_loss": sum(e["total_loss"] or 0 for e in events),
            }


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    
    # Test ledger
    config = ArbConfig()
    config.db_path = "test_pm_15m_ledger.db"
    
    ledger = Ledger(config)
    
    print("\n=== Ledger Module ===\n")
    print(f"Database: {ledger.db_path}")
    print(f"Session ID: {ledger.session_id}")
    
    # Log a test window
    ledger.log_window(
        market_id="test_market",
        window_id="2024-01-15_12:00",
        position={
            "qty_yes": 10,
            "qty_no": 10,
            "cost_yes": 4.8,
            "cost_no": 4.9,
            "trades_count": 2,
            "pairs_filled": 2,
            "legging_events": 0,
        },
        safe_profit_net=0.30,
        theoretical_edge=0.035,
        signals_seen=5,
        signals_taken=2,
    )
    
    print("\nSession summary:", ledger.get_session_summary())
    print("Overall stats:", ledger.get_stats_summary())

