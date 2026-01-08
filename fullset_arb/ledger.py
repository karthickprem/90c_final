"""
Ledger Module - SQLite Storage for Full-Set Arbitrage Bot

Persists:
- Opportunities detected (all, including non-actionable)
- Executions (fills, unwinds, P&L)
- Positions (open full-sets awaiting settlement)
- Daily summaries

Enables analysis of:
- Opportunity frequency
- Fill rate
- One-leg rate
- Average edge captured
- Net P&L curve
- Worst day / max drawdown
"""

import sqlite3
import logging
import json
from typing import List, Dict, Optional, Any
from datetime import datetime, date
from dataclasses import asdict
from pathlib import Path
from contextlib import contextmanager

from .config import ArbConfig, load_config
from .scanner import ArbOpportunity
from .executor import ExecutionResult, ExecutionStatus

logger = logging.getLogger(__name__)


class Ledger:
    """
    SQLite-based ledger for full-set arbitrage bot.
    
    Tables:
    - opportunities: All detected opportunities
    - executions: Execution results (fills, unwinds)
    - positions: Open positions awaiting settlement
    - daily_summary: Aggregated daily stats
    """
    
    def __init__(self, config: ArbConfig = None):
        self.config = config or load_config()
        self.db_path = self.config.db_path
        self._init_db()
    
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
                -- Detected opportunities
                CREATE TABLE IF NOT EXISTS opportunities (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    timestamp TEXT NOT NULL,
                    market_id TEXT NOT NULL,
                    market_slug TEXT NOT NULL,
                    market_question TEXT,
                    ask_yes REAL NOT NULL,
                    ask_no REAL NOT NULL,
                    bid_yes REAL,
                    bid_no REAL,
                    edge REAL NOT NULL,
                    edge_after_fees REAL NOT NULL,
                    depth_yes REAL,
                    depth_no REAL,
                    min_depth REAL,
                    spread_yes REAL,
                    spread_no REAL,
                    is_actionable INTEGER NOT NULL,
                    reject_reason TEXT,
                    was_executed INTEGER DEFAULT 0
                );
                
                -- Execution results
                CREATE TABLE IF NOT EXISTS executions (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    opportunity_id INTEGER,
                    timestamp TEXT NOT NULL,
                    market_id TEXT NOT NULL,
                    market_slug TEXT NOT NULL,
                    status TEXT NOT NULL,
                    
                    -- YES leg
                    yes_target_shares REAL,
                    yes_target_price REAL,
                    yes_filled_shares REAL,
                    yes_fill_price REAL,
                    yes_status TEXT,
                    
                    -- NO leg
                    no_target_shares REAL,
                    no_target_price REAL,
                    no_filled_shares REAL,
                    no_fill_price REAL,
                    no_status TEXT,
                    
                    -- Unwind (if one-leg)
                    unwind_leg TEXT,
                    unwind_price REAL,
                    unwind_shares REAL,
                    unwind_loss REAL,
                    unwind_loss_pct REAL,
                    
                    -- P&L
                    total_cost REAL,
                    expected_payout REAL,
                    realized_pnl REAL,
                    unrealized_pnl REAL,
                    
                    -- Execution quality
                    edge_at_execution REAL,
                    total_time_ms REAL,
                    
                    FOREIGN KEY (opportunity_id) REFERENCES opportunities(id)
                );
                
                -- Open positions (full sets held)
                CREATE TABLE IF NOT EXISTS positions (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    execution_id INTEGER NOT NULL,
                    market_id TEXT NOT NULL,
                    market_slug TEXT NOT NULL,
                    opened_at TEXT NOT NULL,
                    shares REAL NOT NULL,
                    cost REAL NOT NULL,
                    expected_payout REAL NOT NULL,
                    status TEXT DEFAULT 'open',
                    closed_at TEXT,
                    settlement_value REAL,
                    realized_pnl REAL,
                    FOREIGN KEY (execution_id) REFERENCES executions(id)
                );
                
                -- Daily summary
                CREATE TABLE IF NOT EXISTS daily_summary (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    date TEXT UNIQUE NOT NULL,
                    opportunities_total INTEGER DEFAULT 0,
                    opportunities_actionable INTEGER DEFAULT 0,
                    executions_total INTEGER DEFAULT 0,
                    executions_success INTEGER DEFAULT 0,
                    executions_one_leg INTEGER DEFAULT 0,
                    executions_failed INTEGER DEFAULT 0,
                    total_pnl REAL DEFAULT 0,
                    total_unwind_loss REAL DEFAULT 0,
                    best_edge REAL,
                    avg_edge REAL,
                    avg_fill_rate REAL,
                    updated_at TEXT
                );
                
                -- Indexes
                CREATE INDEX IF NOT EXISTS idx_opp_timestamp ON opportunities(timestamp);
                CREATE INDEX IF NOT EXISTS idx_opp_market ON opportunities(market_id);
                CREATE INDEX IF NOT EXISTS idx_opp_actionable ON opportunities(is_actionable);
                CREATE INDEX IF NOT EXISTS idx_exec_timestamp ON executions(timestamp);
                CREATE INDEX IF NOT EXISTS idx_exec_status ON executions(status);
                CREATE INDEX IF NOT EXISTS idx_pos_status ON positions(status);
                CREATE INDEX IF NOT EXISTS idx_daily_date ON daily_summary(date);
            """)
    
    def log_opportunity(self, opp: ArbOpportunity) -> int:
        """Log a detected opportunity. Returns the opportunity ID."""
        with self._connection() as conn:
            cursor = conn.execute("""
                INSERT INTO opportunities (
                    timestamp, market_id, market_slug, market_question,
                    ask_yes, ask_no, bid_yes, bid_no,
                    edge, edge_after_fees,
                    depth_yes, depth_no, min_depth,
                    spread_yes, spread_no,
                    is_actionable, reject_reason
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                opp.timestamp.isoformat(),
                opp.market.market_id,
                opp.market.slug,
                opp.market.question[:200] if opp.market.question else None,
                opp.ask_yes,
                opp.ask_no,
                opp.bid_yes,
                opp.bid_no,
                opp.edge_l1,      # Use edge_l1 (was: edge)
                opp.edge_exec,    # Use edge_exec (was: edge_after_fees)
                opp.depth_yes,
                opp.depth_no,
                opp.min_depth,
                opp.spread_yes,
                opp.spread_no,
                1 if opp.is_actionable else 0,
                opp.reject_reason,
            ))
            return cursor.lastrowid
    
    def log_execution(self, result: ExecutionResult, opportunity_id: int = None) -> int:
        """Log an execution result. Returns the execution ID."""
        with self._connection() as conn:
            cursor = conn.execute("""
                INSERT INTO executions (
                    opportunity_id, timestamp, market_id, market_slug, status,
                    yes_target_shares, yes_target_price, yes_filled_shares, yes_fill_price, yes_status,
                    no_target_shares, no_target_price, no_filled_shares, no_fill_price, no_status,
                    unwind_leg, unwind_price, unwind_shares, unwind_loss, unwind_loss_pct,
                    total_cost, expected_payout, realized_pnl, unrealized_pnl,
                    edge_at_execution, total_time_ms
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                opportunity_id,
                result.timestamp.isoformat(),
                result.opportunity.market.market_id,
                result.opportunity.market.slug,
                result.status.value,
                # YES leg
                result.yes_fill.target_shares if result.yes_fill else None,
                result.yes_fill.target_price if result.yes_fill else None,
                result.yes_fill.filled_shares if result.yes_fill else None,
                result.yes_fill.fill_price if result.yes_fill else None,
                result.yes_fill.status.value if result.yes_fill else None,
                # NO leg
                result.no_fill.target_shares if result.no_fill else None,
                result.no_fill.target_price if result.no_fill else None,
                result.no_fill.filled_shares if result.no_fill else None,
                result.no_fill.fill_price if result.no_fill else None,
                result.no_fill.status.value if result.no_fill else None,
                # Unwind (use unwind_vwap instead of unwind_price)
                result.unwind.leg.side if result.unwind else None,
                result.unwind.unwind_vwap if result.unwind else None,
                result.unwind.unwind_shares if result.unwind else None,
                result.unwind.unwind_loss if result.unwind else None,
                result.unwind.unwind_loss_pct if result.unwind else None,
                # P&L (use redemption_value instead of expected_payout, no unrealized for instant redeem)
                result.total_cost,
                result.redemption_value,  # Was: expected_payout
                result.realized_pnl,
                0,  # No unrealized PnL with instant redemption
                result.opportunity.edge_exec,  # Was: edge
                result.total_time_ms,
            ))
            
            # Mark opportunity as executed
            if opportunity_id:
                conn.execute(
                    "UPDATE opportunities SET was_executed = 1 WHERE id = ?",
                    (opportunity_id,)
                )
            
            return cursor.lastrowid
    
    def create_position(self, execution_id: int, result: ExecutionResult) -> int:
        """Create an open position for a successful full-set execution."""
        if result.status != ExecutionStatus.SUCCESS:
            return None
        
        with self._connection() as conn:
            cursor = conn.execute("""
                INSERT INTO positions (
                    execution_id, market_id, market_slug, opened_at,
                    shares, cost, expected_payout, status
                ) VALUES (?, ?, ?, ?, ?, ?, ?, 'open')
            """, (
                execution_id,
                result.opportunity.market.market_id,
                result.opportunity.market.slug,
                result.timestamp.isoformat(),
                min(result.yes_fill.filled_shares, result.no_fill.filled_shares),
                result.total_cost,
                result.expected_payout,
            ))
            return cursor.lastrowid
    
    def close_position(self, position_id: int, settlement_value: float):
        """Close a position with settlement value."""
        with self._connection() as conn:
            # Get position cost
            row = conn.execute(
                "SELECT cost FROM positions WHERE id = ?", (position_id,)
            ).fetchone()
            
            if row:
                cost = row["cost"]
                realized_pnl = settlement_value - cost
                
                conn.execute("""
                    UPDATE positions SET
                        status = 'closed',
                        closed_at = ?,
                        settlement_value = ?,
                        realized_pnl = ?
                    WHERE id = ?
                """, (datetime.now().isoformat(), settlement_value, realized_pnl, position_id))
    
    def get_open_positions(self) -> List[Dict]:
        """Get all open positions."""
        with self._connection() as conn:
            rows = conn.execute("""
                SELECT * FROM positions WHERE status = 'open'
                ORDER BY opened_at DESC
            """).fetchall()
            return [dict(row) for row in rows]
    
    def update_daily_summary(self):
        """Update daily summary for today."""
        today = date.today().isoformat()
        
        with self._connection() as conn:
            # Get today's stats
            opp_stats = conn.execute("""
                SELECT 
                    COUNT(*) as total,
                    SUM(CASE WHEN is_actionable = 1 THEN 1 ELSE 0 END) as actionable,
                    MAX(edge) as best_edge,
                    AVG(edge) as avg_edge
                FROM opportunities
                WHERE date(timestamp) = ?
            """, (today,)).fetchone()
            
            exec_stats = conn.execute("""
                SELECT 
                    COUNT(*) as total,
                    SUM(CASE WHEN status = 'SUCCESS' THEN 1 ELSE 0 END) as success,
                    SUM(CASE WHEN status = 'ONE_LEG' THEN 1 ELSE 0 END) as one_leg,
                    SUM(CASE WHEN status = 'BOTH_FAILED' THEN 1 ELSE 0 END) as failed,
                    SUM(realized_pnl) + SUM(unrealized_pnl) as total_pnl,
                    SUM(unwind_loss) as total_unwind
                FROM executions
                WHERE date(timestamp) = ?
            """, (today,)).fetchone()
            
            # Calculate fill rate
            total_exec = exec_stats["total"] or 0
            success = exec_stats["success"] or 0
            fill_rate = (success / total_exec * 100) if total_exec > 0 else 0
            
            # Upsert summary
            conn.execute("""
                INSERT INTO daily_summary (
                    date, opportunities_total, opportunities_actionable,
                    executions_total, executions_success, executions_one_leg, executions_failed,
                    total_pnl, total_unwind_loss, best_edge, avg_edge, avg_fill_rate, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(date) DO UPDATE SET
                    opportunities_total = excluded.opportunities_total,
                    opportunities_actionable = excluded.opportunities_actionable,
                    executions_total = excluded.executions_total,
                    executions_success = excluded.executions_success,
                    executions_one_leg = excluded.executions_one_leg,
                    executions_failed = excluded.executions_failed,
                    total_pnl = excluded.total_pnl,
                    total_unwind_loss = excluded.total_unwind_loss,
                    best_edge = excluded.best_edge,
                    avg_edge = excluded.avg_edge,
                    avg_fill_rate = excluded.avg_fill_rate,
                    updated_at = excluded.updated_at
            """, (
                today,
                opp_stats["total"] or 0,
                opp_stats["actionable"] or 0,
                exec_stats["total"] or 0,
                exec_stats["success"] or 0,
                exec_stats["one_leg"] or 0,
                exec_stats["failed"] or 0,
                exec_stats["total_pnl"] or 0,
                exec_stats["total_unwind"] or 0,
                opp_stats["best_edge"],
                opp_stats["avg_edge"],
                fill_rate,
                datetime.now().isoformat(),
            ))
    
    def get_daily_summaries(self, days: int = 30) -> List[Dict]:
        """Get daily summaries for past N days."""
        with self._connection() as conn:
            rows = conn.execute("""
                SELECT * FROM daily_summary
                ORDER BY date DESC
                LIMIT ?
            """, (days,)).fetchall()
            return [dict(row) for row in rows]
    
    def get_pnl_curve(self, days: int = 30) -> List[Dict]:
        """Get cumulative P&L curve for past N days."""
        summaries = self.get_daily_summaries(days)
        
        # Reverse to get chronological order
        summaries.reverse()
        
        cumulative_pnl = 0
        curve = []
        
        for s in summaries:
            cumulative_pnl += s.get("total_pnl", 0) or 0
            curve.append({
                "date": s["date"],
                "daily_pnl": s.get("total_pnl", 0) or 0,
                "cumulative_pnl": cumulative_pnl,
            })
        
        return curve
    
    def get_stats_summary(self) -> Dict:
        """Get overall statistics summary."""
        with self._connection() as conn:
            # Overall stats
            overall = conn.execute("""
                SELECT 
                    COUNT(*) as total_opportunities,
                    SUM(CASE WHEN is_actionable = 1 THEN 1 ELSE 0 END) as actionable,
                    AVG(edge) as avg_edge,
                    MAX(edge) as best_edge
                FROM opportunities
            """).fetchone()
            
            exec_overall = conn.execute("""
                SELECT 
                    COUNT(*) as total_executions,
                    SUM(CASE WHEN status = 'SUCCESS' THEN 1 ELSE 0 END) as successes,
                    SUM(CASE WHEN status = 'ONE_LEG' THEN 1 ELSE 0 END) as one_legs,
                    SUM(realized_pnl) + SUM(unrealized_pnl) as total_pnl,
                    SUM(unwind_loss) as total_unwind_loss,
                    MIN(realized_pnl) as worst_trade,
                    MAX(unrealized_pnl) as best_trade
                FROM executions
            """).fetchone()
            
            # Max drawdown calculation
            curve = self.get_pnl_curve(365)
            max_drawdown = 0
            peak = 0
            for point in curve:
                cum = point["cumulative_pnl"]
                if cum > peak:
                    peak = cum
                drawdown = peak - cum
                if drawdown > max_drawdown:
                    max_drawdown = drawdown
            
            total_exec = exec_overall["total_executions"] or 0
            successes = exec_overall["successes"] or 0
            
            return {
                "total_opportunities": overall["total_opportunities"] or 0,
                "actionable_opportunities": overall["actionable"] or 0,
                "avg_edge": overall["avg_edge"] or 0,
                "best_edge": overall["best_edge"] or 0,
                "total_executions": total_exec,
                "successful_executions": successes,
                "one_leg_executions": exec_overall["one_legs"] or 0,
                "success_rate": (successes / total_exec * 100) if total_exec > 0 else 0,
                "total_pnl": exec_overall["total_pnl"] or 0,
                "total_unwind_loss": exec_overall["total_unwind_loss"] or 0,
                "worst_trade": exec_overall["worst_trade"] or 0,
                "best_trade": exec_overall["best_trade"] or 0,
                "max_drawdown": max_drawdown,
            }


def main():
    """Test ledger."""
    logging.basicConfig(level=logging.INFO)
    
    # Use test database
    config = ArbConfig()
    config.db_path = "test_fullset_arb.db"
    
    ledger = Ledger(config)
    
    print("Ledger initialized")
    print(f"Database: {ledger.db_path}")
    
    # Get stats (will be empty initially)
    stats = ledger.get_stats_summary()
    print("\n=== Stats Summary ===")
    for key, value in stats.items():
        print(f"  {key}: {value}")


if __name__ == "__main__":
    main()

