"""
Logging Utilities

JSONL trade logging and daily summaries.
"""

from __future__ import annotations

import json
import os
from datetime import datetime
from pathlib import Path
from typing import Dict, Any, List


class TradeLogger:
    """
    Logs trades to JSONL and generates daily summaries.
    """
    
    def __init__(self, outdir: str = "out_paper"):
        self.outdir = Path(outdir)
        self.outdir.mkdir(parents=True, exist_ok=True)
        
        self.trades_file = self.outdir / "trades.jsonl"
        self.daily_file = self.outdir / "daily_summary.json"
        
        # In-memory daily tracking
        self.daily_trades: Dict[str, List[Dict]] = {}
    
    def log_trade(self, trade: Dict[str, Any]) -> None:
        """
        Log a single trade to JSONL.
        
        Appends to trades.jsonl with timestamp.
        """
        record = {
            'logged_at': datetime.now().isoformat(),
            **trade
        }
        
        with open(self.trades_file, 'a') as f:
            f.write(json.dumps(record) + '\n')
        
        # Track for daily summary
        date_str = datetime.now().strftime('%Y-%m-%d')
        if date_str not in self.daily_trades:
            self.daily_trades[date_str] = []
        self.daily_trades[date_str].append(trade)
    
    def log_skip(self, window_id: str, reason: str, details: Dict[str, Any] = None) -> None:
        """Log a skipped trade (validation failed, no trigger, etc)."""
        record = {
            'logged_at': datetime.now().isoformat(),
            'window_id': window_id,
            'status': 'SKIPPED',
            'skip_reason': reason,
            'details': details or {},
        }
        
        with open(self.trades_file, 'a') as f:
            f.write(json.dumps(record) + '\n')
    
    def write_daily_summary(self, stats: Dict[str, Any]) -> None:
        """
        Write daily summary JSON.
        
        Includes:
        - Per-day breakdown
        - Aggregate statistics
        """
        summary = {
            'generated_at': datetime.now().isoformat(),
            'aggregate': stats,
            'by_day': {},
        }
        
        # Compute per-day stats
        for date_str, trades in self.daily_trades.items():
            executed = [t for t in trades if t.get('exit_reason') not in 
                       ['SPIKE_FAIL', 'JUMP_FAIL', 'TIE_SKIP', 'FILL_TIMEOUT', None]]
            
            if executed:
                pnls = [t.get('pnl_invested', 0) for t in executed]
                avg_entry = sum(t.get('entry_fill_price', 0) for t in executed) / len(executed)
                avg_exit = sum(t.get('exit_price', 0) for t in executed) / len(executed)
                gap_count = sum(1 for t in executed if t.get('pnl_invested', 0) <= -0.15)
                
                day_stats = {
                    'trades_total': len(trades),
                    'trades_executed': len(executed),
                    'avg_entry': round(avg_entry, 1),
                    'avg_exit': round(avg_exit, 1),
                    'total_pnl': round(sum(pnls) * 100, 2),  # As percentage
                    'worst_trade': round(min(pnls), 4) if pnls else 0,
                    'gap_count': gap_count,
                }
            else:
                day_stats = {
                    'trades_total': len(trades),
                    'trades_executed': 0,
                }
            
            summary['by_day'][date_str] = day_stats
        
        with open(self.daily_file, 'w') as f:
            json.dump(summary, f, indent=2)
    
    def print_summary(self, stats: Dict[str, Any]) -> None:
        """Print formatted summary to console."""
        print("\n" + "=" * 60)
        print("PAPER TRADING SUMMARY")
        print("=" * 60)
        print(f"Trades executed: {stats.get('trades', 0)}")
        print(f"Wins: {stats.get('wins', 0)}")
        print(f"Losses: {stats.get('losses', 0)}")
        print(f"")
        print(f"Starting bankroll: ${stats.get('starting_bankroll', 0):.2f}")
        print(f"Final bankroll:    ${stats.get('bankroll', 0):.2f}")
        print(f"P&L:               ${stats.get('pnl_total', 0):+.2f} ({stats.get('pnl_pct', 0):+.1f}%)")
        print(f"")
        print(f"Avg entry: {stats.get('avg_entry', 0):.1f}c")
        print(f"Avg exit:  {stats.get('avg_exit', 0):.1f}c")
        print(f"")
        print(f"Worst loss: {stats.get('worst_loss', 0):.2%}")
        print(f"Max drawdown: {stats.get('max_drawdown', 0):.2%}")
        print(f"Gap events (<-15%): {stats.get('gap_count', 0)}")
        print(f"Severe gaps (<-25%): {stats.get('severe_count', 0)}")
        print("=" * 60)


