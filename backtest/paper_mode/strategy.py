"""
Strategy State Machine

Implements the FINAL PRODUCTION CONFIG exactly:
- SPIKE validation
- JUMP gate (combined big + mid)
- Paper fill simulation
- Exit logic (TP/SL/Settlement)
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum, auto
from typing import List, Optional, Dict, Any
import time

from .config import StrategyConfig


class State(Enum):
    """Strategy state machine states."""
    IDLE = auto()           # Waiting for first-touch
    OBSERVE_10S = auto()    # Collecting validation window samples
    ENTRY_PENDING = auto()  # Simulated limit order, waiting up to 2s
    IN_POSITION = auto()    # Monitoring for TP/SL/Settlement
    DONE = auto()           # Lockout until next window


@dataclass
class Tick:
    """
    A single price sample with full bid/ask data.
    
    For realistic paper trading:
    - Use ASK for entry trigger/fill (price you pay to BUY)
    - Use BID for exit/sell (price you receive when SELL)
    """
    ts: float               # Unix timestamp
    
    # UP side prices (cents)
    up_bid: int = 0         # Best bid to SELL UP
    up_ask: int = 0         # Best ask to BUY UP
    up_cents: int = 0       # Midpoint (legacy, for logging)
    
    # DOWN side prices (cents)
    down_bid: int = 0       # Best bid to SELL DOWN
    down_ask: int = 0       # Best ask to BUY DOWN
    down_cents: int = 0     # Midpoint (legacy, for logging)
    
    # Metadata
    is_synthetic: bool = False  # True if bid/ask computed from mid+spread
    
    @property
    def up_spread(self) -> int:
        return self.up_ask - self.up_bid
    
    @property
    def down_spread(self) -> int:
        return self.down_ask - self.down_bid
    
    def is_valid(self) -> bool:
        """Check if tick has valid prices."""
        # At minimum, need ask prices for trigger/entry
        up_valid = 0 <= self.up_ask <= 100
        down_valid = 0 <= self.down_ask <= 100
        return up_valid and down_valid
    
    def get_buy_price(self, side: str) -> int:
        """Get the price to BUY (ask) for a side."""
        return self.up_ask if side == 'UP' else self.down_ask
    
    def get_sell_price(self, side: str) -> int:
        """Get the price to SELL (bid) for a side."""
        return self.up_bid if side == 'UP' else self.down_bid
    
    def get_mid_price(self, side: str) -> int:
        """Get midpoint for logging."""
        return self.up_cents if side == 'UP' else self.down_cents


@dataclass
class WindowContext:
    """Tracks state for a single 15-min window."""
    window_id: str
    window_start: float     # Unix timestamp of window start
    window_end: float       # Unix timestamp of window end
    
    # All ticks in this window
    ticks: List[Tick] = field(default_factory=list)
    
    # Trigger info
    trigger_ts: Optional[float] = None
    trigger_side: Optional[str] = None  # 'UP' or 'DOWN'
    trigger_price: Optional[int] = None
    
    # Validation window (10s after trigger)
    validation_ticks: List[Tick] = field(default_factory=list)
    spike_min_side: Optional[int] = None
    spike_max_side: Optional[int] = None
    jump_max_abs_delta: int = 0
    jump_mid_count: int = 0
    
    # Entry
    entry_submitted_ts: Optional[float] = None
    entry_fill_ts: Optional[float] = None
    entry_fill_price: Optional[int] = None
    entry_slip: int = 0
    shares: float = 0.0
    dollars_invested: float = 0.0
    
    # Exit
    exit_reason: Optional[str] = None  # 'TP', 'SL', 'SETTLEMENT_WIN', 'SETTLEMENT_LOSS', 'SKIPPED', 'FILL_TIMEOUT', 'VALIDATION_FAIL'
    exit_ts: Optional[float] = None
    exit_price: Optional[int] = None
    exit_slip: int = 0
    
    # P&L
    realized_pnl_invested: float = 0.0  # (exit - entry) / entry
    realized_pnl_dollars: float = 0.0
    
    # Settlement
    settle_winner: Optional[str] = None


class StrategyStateMachine:
    """
    State machine for the production strategy.
    
    Usage:
        sm = StrategyStateMachine(config)
        sm.start_window(window_id, start_ts, end_ts)
        
        while window_active:
            tick = get_tick_from_api()
            result = sm.process_tick(tick, bankroll)
            
            if result.get('trade_complete'):
                log_trade(sm.context)
    """
    
    def __init__(self, config: StrategyConfig):
        self.config = config
        self.state = State.IDLE
        self.context: Optional[WindowContext] = None
    
    def start_window(self, window_id: str, start_ts: float, end_ts: float) -> None:
        """Initialize for a new window."""
        self.state = State.IDLE
        self.context = WindowContext(
            window_id=window_id,
            window_start=start_ts,
            window_end=end_ts,
        )
    
    def process_tick(self, tick: Tick, bankroll: float) -> Dict[str, Any]:
        """
        Process a new tick and return status.
        
        Returns dict with:
            - state: current state name
            - trade_complete: True if trade finished (success or skip)
            - entry: True if entry just happened
            - exit: True if exit just happened
            - reason: skip/exit reason if applicable
        """
        if not tick.is_valid():
            return {'state': self.state.name, 'trade_complete': False}
        
        if self.context is None:
            return {'state': 'NO_CONTEXT', 'trade_complete': False}
        
        # Add to window ticks
        self.context.ticks.append(tick)
        
        result = {'state': self.state.name, 'trade_complete': False}
        
        if self.state == State.IDLE:
            result = self._handle_idle(tick)
        
        elif self.state == State.OBSERVE_10S:
            result = self._handle_observe(tick, bankroll)
        
        elif self.state == State.ENTRY_PENDING:
            result = self._handle_entry_pending(tick, bankroll)
        
        elif self.state == State.IN_POSITION:
            result = self._handle_in_position(tick)
        
        elif self.state == State.DONE:
            result = {'state': 'DONE', 'trade_complete': True}
        
        return result
    
    def force_settle(self, winner: Optional[str]) -> Dict[str, Any]:
        """Force settlement at window end."""
        if self.state != State.IN_POSITION:
            return {'state': self.state.name, 'trade_complete': True}
        
        self.context.settle_winner = winner
        
        if winner == self.context.trigger_side:
            self.context.exit_reason = 'SETTLEMENT_WIN'
            self.context.exit_price = 100
        else:
            self.context.exit_reason = 'SETTLEMENT_LOSS'
            self.context.exit_price = 0
        
        self.context.exit_ts = time.time()
        self._compute_pnl()
        self.state = State.DONE
        
        return {
            'state': 'DONE',
            'trade_complete': True,
            'exit': True,
            'reason': self.context.exit_reason,
        }
    
    def _handle_idle(self, tick: Tick) -> Dict[str, Any]:
        """
        Look for first-touch trigger.
        
        IMPORTANT: Use ASK price for trigger (price we'd pay to buy).
        """
        # Use ASK for trigger detection (realistic entry price)
        up_touched = tick.up_ask >= self.config.trigger_threshold
        down_touched = tick.down_ask >= self.config.trigger_threshold
        
        if up_touched and down_touched:
            # TIE - skip this window
            self.context.exit_reason = 'TIE_SKIP'
            self.state = State.DONE
            return {'state': 'DONE', 'trade_complete': True, 'reason': 'TIE_SKIP'}
        
        if up_touched:
            self._trigger('UP', tick)
            return {'state': 'OBSERVE_10S', 'trade_complete': False, 'triggered': True}
        
        if down_touched:
            self._trigger('DOWN', tick)
            return {'state': 'OBSERVE_10S', 'trade_complete': False, 'triggered': True}
        
        return {'state': 'IDLE', 'trade_complete': False}
    
    def _trigger(self, side: str, tick: Tick) -> None:
        """
        Record trigger and start validation window.
        
        Use ASK price as trigger price (what we'd pay to enter).
        """
        self.context.trigger_ts = tick.ts
        self.context.trigger_side = side
        # Use ASK price (tradable buy price)
        self.context.trigger_price = tick.get_buy_price(side)
        self.state = State.OBSERVE_10S
    
    def _handle_observe(self, tick: Tick, bankroll: float) -> Dict[str, Any]:
        """
        Collect validation window samples and check SPIKE + JUMP gates.
        
        For SPIKE: use ASK price (what we'd pay to enter).
        For JUMP: use ASK prices for delta calculation.
        """
        elapsed = tick.ts - self.context.trigger_ts
        
        # Add to validation ticks
        self.context.validation_ticks.append(tick)
        
        # Update SPIKE metrics (chosen side only, using ASK = tradable buy price)
        side_price = tick.get_buy_price(self.context.trigger_side)
        
        if self.context.spike_min_side is None:
            self.context.spike_min_side = side_price
            self.context.spike_max_side = side_price
        else:
            self.context.spike_min_side = min(self.context.spike_min_side, side_price)
            self.context.spike_max_side = max(self.context.spike_max_side, side_price)
        
        # Update JUMP metrics (both sides, tick-to-tick deltas on ASK prices)
        if len(self.context.validation_ticks) >= 2:
            prev = self.context.validation_ticks[-2]
            curr = tick
            
            # Use ASK for delta calculation (matches entry price semantics)
            delta_up = abs(curr.up_ask - prev.up_ask)
            delta_down = abs(curr.down_ask - prev.down_ask)
            max_delta = max(delta_up, delta_down)
            
            self.context.jump_max_abs_delta = max(self.context.jump_max_abs_delta, max_delta)
            
            if max_delta >= self.config.mid_jump:
                self.context.jump_mid_count += 1
        
        # Check if validation window complete (10s elapsed)
        if elapsed >= self.config.validation_secs:
            return self._complete_validation(tick, bankroll)
        
        return {'state': 'OBSERVE_10S', 'trade_complete': False, 'elapsed': elapsed}
    
    def _complete_validation(self, tick: Tick, bankroll: float) -> Dict[str, Any]:
        """Check gates and transition to entry or skip."""
        
        # SPIKE check
        spike_ok = (
            self.context.spike_min_side >= self.config.spike_min and
            self.context.spike_max_side >= self.config.spike_max
        )
        
        # JUMP check
        jump_ok = (
            self.context.jump_max_abs_delta < self.config.big_jump and
            self.context.jump_mid_count < self.config.max_mid_count
        )
        
        if not spike_ok:
            self.context.exit_reason = 'SPIKE_FAIL'
            self.state = State.DONE
            return {'state': 'DONE', 'trade_complete': True, 'reason': 'SPIKE_FAIL'}
        
        if not jump_ok:
            self.context.exit_reason = 'JUMP_FAIL'
            self.state = State.DONE
            return {'state': 'DONE', 'trade_complete': True, 'reason': 'JUMP_FAIL'}
        
        # Submit entry order
        self.context.entry_submitted_ts = tick.ts
        self.state = State.ENTRY_PENDING
        
        return self._try_fill(tick, bankroll)
    
    def _handle_entry_pending(self, tick: Tick, bankroll: float) -> Dict[str, Any]:
        """Wait for fill or timeout."""
        elapsed = tick.ts - self.context.entry_submitted_ts
        
        if elapsed >= self.config.fill_timeout_secs:
            self.context.exit_reason = 'FILL_TIMEOUT'
            self.state = State.DONE
            return {'state': 'DONE', 'trade_complete': True, 'reason': 'FILL_TIMEOUT'}
        
        return self._try_fill(tick, bankroll)
    
    def _try_fill(self, tick: Tick, bankroll: float) -> Dict[str, Any]:
        """
        Attempt to fill at current price with slippage model.
        
        Use ASK price for entry (price we pay to buy).
        """
        # Use ASK price (tradable buy price)
        side_ask = tick.get_buy_price(self.context.trigger_side)
        
        # Apply slippage ON TOP of ask (conservative: assume we pay a bit more)
        fill_price = side_ask + self.config.slip_entry
        
        # Check p_max - can only fill if ask <= p_max (since we buy at ask)
        if side_ask > self.config.p_max:
            # Can't fill at this price, keep waiting
            return {'state': 'ENTRY_PENDING', 'trade_complete': False, 'ask': side_ask}
        
        # Fill!
        self.context.entry_fill_ts = tick.ts
        # Fill at min(ask + slip, p_max)
        self.context.entry_fill_price = min(fill_price, self.config.p_max)
        self.context.entry_slip = self.context.entry_fill_price - side_ask
        
        # Calculate position
        self.context.dollars_invested = bankroll * self.config.f
        self.context.shares = self.context.dollars_invested / (self.context.entry_fill_price / 100.0)
        
        self.state = State.IN_POSITION
        
        return {'state': 'IN_POSITION', 'trade_complete': False, 'entry': True, 'fill_price': self.context.entry_fill_price}
    
    def _handle_in_position(self, tick: Tick) -> Dict[str, Any]:
        """
        Monitor for TP/SL exits.
        
        Use BID price for exit checks (price we receive when selling).
        """
        # Use BID for exit (tradable sell price)
        side_bid = tick.get_sell_price(self.context.trigger_side)
        
        # Check TP - exit when BID reaches TP level
        if side_bid >= self.config.tp:
            self.context.exit_reason = 'TP'
            # TP is a limit sell, execute at TP price
            self.context.exit_price = self.config.tp
            self.context.exit_slip = 0  # Limit order at TP
            self.context.exit_ts = tick.ts
            self._compute_pnl()
            self.state = State.DONE
            return {'state': 'DONE', 'trade_complete': True, 'exit': True, 'reason': 'TP', 'exit_price': self.config.tp}
        
        # Check SL - exit when BID drops to SL level
        if side_bid <= self.config.sl:
            # SL is marketable sell, apply slippage against us
            exit_price = max(0, side_bid - self.config.slip_exit)
            self.context.exit_reason = 'SL'
            self.context.exit_price = exit_price
            self.context.exit_slip = self.config.slip_exit
            self.context.exit_ts = tick.ts
            self._compute_pnl()
            self.state = State.DONE
            return {'state': 'DONE', 'trade_complete': True, 'exit': True, 'reason': 'SL', 'exit_price': exit_price}
        
        return {'state': 'IN_POSITION', 'trade_complete': False, 'bid': side_bid}
    
    def _compute_pnl(self) -> None:
        """Compute realized P&L."""
        if self.context.entry_fill_price is None or self.context.exit_price is None:
            return
        
        entry = self.context.entry_fill_price
        exit_p = self.context.exit_price
        
        # PnL per share (in cents)
        pnl_cents = exit_p - entry
        
        # PnL as fraction of invested
        self.context.realized_pnl_invested = pnl_cents / entry
        
        # PnL in dollars
        self.context.realized_pnl_dollars = self.context.shares * (pnl_cents / 100.0)
    
    def get_trade_record(self) -> Dict[str, Any]:
        """Get complete trade record for logging."""
        if self.context is None:
            return {}
        
        c = self.context
        
        # Calculate average spread during validation window
        avg_up_spread = 0
        avg_down_spread = 0
        if c.validation_ticks:
            up_spreads = [t.up_spread for t in c.validation_ticks]
            down_spreads = [t.down_spread for t in c.validation_ticks]
            avg_up_spread = sum(up_spreads) / len(up_spreads) if up_spreads else 0
            avg_down_spread = sum(down_spreads) / len(down_spreads) if down_spreads else 0
        
        return {
            'window_id': c.window_id,
            'trigger_side': c.trigger_side,
            'trigger_price': c.trigger_price,  # Now ASK price
            'trigger_ts': c.trigger_ts,
            'spike_min': c.spike_min_side,
            'spike_max': c.spike_max_side,
            'jump_max_delta': c.jump_max_abs_delta,
            'jump_mid_count': c.jump_mid_count,
            'entry_fill_price': c.entry_fill_price,
            'entry_slip': c.entry_slip,
            'shares': c.shares,
            'dollars_invested': c.dollars_invested,
            'exit_reason': c.exit_reason,
            'exit_price': c.exit_price,
            'exit_slip': c.exit_slip,
            'pnl_invested': c.realized_pnl_invested,
            'pnl_dollars': c.realized_pnl_dollars,
            'settle_winner': c.settle_winner,
            'ticks_count': len(c.ticks),
            'avg_up_spread': avg_up_spread,
            'avg_down_spread': avg_down_spread,
        }

