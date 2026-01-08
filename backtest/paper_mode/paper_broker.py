"""
Paper Broker

Simulates order fills and tracks portfolio without real orders.
Uses conservative slippage model.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import List, Dict, Any, Optional
import time


@dataclass
class Position:
    """An open position."""
    side: str  # 'UP' or 'DOWN'
    entry_price: int  # cents
    shares: float
    cost: float  # dollars invested
    entry_ts: float


@dataclass
class Trade:
    """A completed trade."""
    window_id: str
    side: str
    entry_price: int
    exit_price: int
    shares: float
    cost: float
    proceeds: float
    pnl_dollars: float
    pnl_invested: float  # pnl / cost
    entry_ts: float
    exit_ts: float
    exit_reason: str
    is_gap: bool  # pnl_invested <= -0.15
    is_severe: bool  # pnl_invested <= -0.25


class PaperBroker:
    """
    Paper trading broker.
    
    Simulates fills with slippage model:
    - Entry: fill at min(trigger_price + slip_entry, p_max)
    - Exit TP: fill at TP (limit sell, no slip)
    - Exit SL: fill at tick_price - slip_exit
    - Settlement: fill at 100c (win) or 0c (loss)
    """
    
    def __init__(self, starting_bankroll: float = 100.0):
        self.starting_bankroll = starting_bankroll
        self.bankroll = starting_bankroll
        self.position: Optional[Position] = None
        self.trades: List[Trade] = []
        self.peak_bankroll = starting_bankroll
        self.max_drawdown = 0.0
    
    def get_bankroll(self) -> float:
        """Current available bankroll."""
        return self.bankroll
    
    def can_trade(self) -> bool:
        """Check if we can open a new position."""
        return self.position is None and self.bankroll > 0
    
    def open_position(
        self,
        window_id: str,
        side: str,
        fill_price: int,
        f: float = 0.02,
    ) -> Optional[Position]:
        """
        Open a paper position.
        
        Args:
            window_id: Current window identifier
            side: 'UP' or 'DOWN'
            fill_price: Simulated fill price in cents
            f: Fraction of bankroll to use
        
        Returns:
            Position if opened, None if failed
        """
        if not self.can_trade():
            return None
        
        # Calculate position size
        dollars_to_invest = self.bankroll * f
        shares = dollars_to_invest / (fill_price / 100.0)
        
        self.position = Position(
            side=side,
            entry_price=fill_price,
            shares=shares,
            cost=dollars_to_invest,
            entry_ts=time.time(),
        )
        
        # Deduct from bankroll (we've "spent" this)
        self.bankroll -= dollars_to_invest
        
        return self.position
    
    def close_position(
        self,
        window_id: str,
        exit_price: int,
        exit_reason: str,
    ) -> Optional[Trade]:
        """
        Close the current position.
        
        Args:
            window_id: Current window identifier
            exit_price: Exit price in cents
            exit_reason: 'TP', 'SL', 'SETTLEMENT_WIN', 'SETTLEMENT_LOSS'
        
        Returns:
            Trade record if closed, None if no position
        """
        if self.position is None:
            return None
        
        pos = self.position
        
        # Calculate proceeds
        proceeds = pos.shares * (exit_price / 100.0)
        pnl_dollars = proceeds - pos.cost
        pnl_invested = pnl_dollars / pos.cost if pos.cost > 0 else 0
        
        # Create trade record
        trade = Trade(
            window_id=window_id,
            side=pos.side,
            entry_price=pos.entry_price,
            exit_price=exit_price,
            shares=pos.shares,
            cost=pos.cost,
            proceeds=proceeds,
            pnl_dollars=pnl_dollars,
            pnl_invested=pnl_invested,
            entry_ts=pos.entry_ts,
            exit_ts=time.time(),
            exit_reason=exit_reason,
            is_gap=pnl_invested <= -0.15,
            is_severe=pnl_invested <= -0.25,
        )
        
        self.trades.append(trade)
        
        # Add proceeds back to bankroll
        self.bankroll += proceeds
        
        # Update drawdown tracking
        self.peak_bankroll = max(self.peak_bankroll, self.bankroll)
        current_dd = (self.peak_bankroll - self.bankroll) / self.peak_bankroll
        self.max_drawdown = max(self.max_drawdown, current_dd)
        
        # Clear position
        self.position = None
        
        return trade
    
    def get_stats(self) -> Dict[str, Any]:
        """Get portfolio statistics."""
        if not self.trades:
            return {
                'trades': 0,
                'bankroll': self.bankroll,
                'pnl_total': 0,
                'pnl_pct': 0,
            }
        
        total_pnl = sum(t.pnl_dollars for t in self.trades)
        wins = sum(1 for t in self.trades if t.pnl_dollars > 0)
        losses = len(self.trades) - wins
        
        pnls = [t.pnl_invested for t in self.trades]
        avg_entry = sum(t.entry_price for t in self.trades) / len(self.trades)
        avg_exit = sum(t.exit_price for t in self.trades) / len(self.trades)
        
        gap_count = sum(1 for t in self.trades if t.is_gap)
        severe_count = sum(1 for t in self.trades if t.is_severe)
        
        worst_loss = min(pnls) if pnls else 0
        
        return {
            'trades': len(self.trades),
            'wins': wins,
            'losses': losses,
            'bankroll': round(self.bankroll, 2),
            'starting_bankroll': self.starting_bankroll,
            'pnl_total': round(total_pnl, 2),
            'pnl_pct': round((self.bankroll / self.starting_bankroll - 1) * 100, 2),
            'avg_entry': round(avg_entry, 1),
            'avg_exit': round(avg_exit, 1),
            'worst_loss': round(worst_loss, 4),
            'max_drawdown': round(self.max_drawdown, 4),
            'gap_count': gap_count,
            'severe_count': severe_count,
        }


