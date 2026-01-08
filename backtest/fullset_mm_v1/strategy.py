"""Ladder + Chase Full-Set Accumulator Strategy."""
from dataclasses import dataclass, field
from typing import List, Optional, Literal
from enum import Enum
import math

from .stream import QuoteTick, WindowData
from .config import StrategyConfig


class LegState(Enum):
    """State of a single leg (UP or DOWN)."""
    QUOTING = "quoting"  # Resting bid posted
    FILLED = "filled"    # Got filled
    CHASING = "chasing"  # Raising bid to complete pair


@dataclass
class LegPosition:
    """Position in one leg (UP or DOWN)."""
    side: Literal["UP", "DOWN"]
    state: LegState = LegState.QUOTING
    
    # Quote level
    quote_bid: int = 0
    
    # Fill info (if filled)
    fill_price: Optional[int] = None
    fill_time: Optional[float] = None
    
    # Chase state
    chase_start_time: Optional[float] = None
    last_chase_step_time: Optional[float] = None
    
    def reset(self):
        """Reset to quoting state."""
        self.state = LegState.QUOTING
        self.fill_price = None
        self.fill_time = None
        self.chase_start_time = None
        self.last_chase_step_time = None


@dataclass
class CompletedPair:
    """A completed full-set pair."""
    window_id: str
    
    # Leg 1 (first fill)
    leg1_side: str
    leg1_price: int
    leg1_time: float
    
    # Leg 2 (second fill)
    leg2_side: str
    leg2_price: int
    leg2_time: float
    
    # Derived
    pair_cost: int = 0
    edge_cents: int = 0
    dt_between_legs: float = 0.0
    completed_via_chase: bool = False
    
    # Settlement
    settled_pnl: float = 0.0  # After fees
    
    def __post_init__(self):
        self.pair_cost = self.leg1_price + self.leg2_price
        self.edge_cents = 100 - self.pair_cost
        self.dt_between_legs = abs(self.leg2_time - self.leg1_time)


@dataclass
class UnwindEvent:
    """An unwind when chase fails."""
    window_id: str
    side: str
    buy_price: int
    buy_time: float
    sell_price: int
    sell_time: float
    pnl_cents: int = 0  # Negative = loss
    
    def __post_init__(self):
        self.pnl_cents = self.sell_price - self.buy_price


@dataclass
class StrategyState:
    """Complete strategy state for a window."""
    window_id: str
    config: StrategyConfig
    
    # Leg states
    up_leg: LegPosition = field(default_factory=lambda: LegPosition(side="UP"))
    down_leg: LegPosition = field(default_factory=lambda: LegPosition(side="DOWN"))
    
    # Results
    completed_pairs: List[CompletedPair] = field(default_factory=list)
    unwind_events: List[UnwindEvent] = field(default_factory=list)
    
    # Tracking
    current_time: float = 0.0
    
    def reset_for_new_pair(self):
        """Reset state to start accumulating a new pair."""
        self.up_leg.reset()
        self.down_leg.reset()


def compute_maker_bid(mid: float, d: int) -> int:
    """Compute maker bid price from mid and offset d."""
    return int(math.floor(mid - d))


def check_fill(
    quote_bid: int,
    ask: int,
    fill_model: str
) -> tuple[bool, int]:
    """Check if a maker bid would fill, and at what price.
    
    Returns:
        (filled: bool, fill_price: int)
    """
    if ask <= quote_bid:
        if fill_model == "price_improve_to_ask":
            # Fill at the better ask price
            return True, ask
        else:  # maker_at_bid
            # Fill at our bid
            return True, quote_bid
    return False, 0


class FullSetAccumulator:
    """Simulates the Ladder + Chase Full-Set Accumulator strategy."""
    
    def __init__(self, config: StrategyConfig):
        self.config = config
    
    def run_window(self, window: WindowData) -> StrategyState:
        """Run strategy on a single window.
        
        Returns completed pairs and unwind events.
        """
        state = StrategyState(
            window_id=window.window_id,
            config=self.config
        )
        
        if not window.ticks:
            return state
        
        # Process each tick
        for tick in window.ticks:
            state.current_time = tick.elapsed_secs
            self._process_tick(state, tick)
        
        # Handle end-of-window cleanup
        self._finalize_window(state, window)
        
        return state
    
    def _process_tick(self, state: StrategyState, tick: QuoteTick):
        """Process a single tick."""
        cfg = self.config
        
        # On first tick, initialize quotes if they're at zero
        if state.up_leg.state == LegState.QUOTING and state.up_leg.quote_bid == 0:
            state.up_leg.quote_bid = compute_maker_bid(tick.up_mid, cfg.d_cents)
        
        if state.down_leg.state == LegState.QUOTING and state.down_leg.quote_bid == 0:
            state.down_leg.quote_bid = compute_maker_bid(tick.down_mid, cfg.d_cents)
        
        # Check for fills FIRST (using resting bids from previous tick)
        self._check_fills(state, tick)
        
        # Handle chasing
        self._process_chase(state, tick)
        
        # Check for pair completion
        self._check_pair_complete(state, tick)
        
        # Update quote levels for NEXT tick if still quoting
        if state.up_leg.state == LegState.QUOTING:
            state.up_leg.quote_bid = compute_maker_bid(tick.up_mid, cfg.d_cents)
        
        if state.down_leg.state == LegState.QUOTING:
            state.down_leg.quote_bid = compute_maker_bid(tick.down_mid, cfg.d_cents)
    
    def _check_fills(self, state: StrategyState, tick: QuoteTick):
        """Check if any quotes got filled."""
        cfg = self.config
        
        # Check UP leg
        if state.up_leg.state in [LegState.QUOTING, LegState.CHASING]:
            filled, price = check_fill(
                state.up_leg.quote_bid,
                tick.up_ask,
                cfg.fill_model
            )
            if filled:
                was_chasing = state.up_leg.state == LegState.CHASING
                state.up_leg.state = LegState.FILLED
                state.up_leg.fill_price = price
                state.up_leg.fill_time = state.current_time
                
                # If other leg is still quoting, start chase on it
                if state.down_leg.state == LegState.QUOTING:
                    self._start_chase(state.down_leg, state.current_time)
        
        # Check DOWN leg
        if state.down_leg.state in [LegState.QUOTING, LegState.CHASING]:
            filled, price = check_fill(
                state.down_leg.quote_bid,
                tick.down_ask,
                cfg.fill_model
            )
            if filled:
                was_chasing = state.down_leg.state == LegState.CHASING
                state.down_leg.state = LegState.FILLED
                state.down_leg.fill_price = price
                state.down_leg.fill_time = state.current_time
                
                # If other leg is still quoting, start chase on it
                if state.up_leg.state == LegState.QUOTING:
                    self._start_chase(state.up_leg, state.current_time)
    
    def _start_chase(self, leg: LegPosition, current_time: float):
        """Start chasing on a leg."""
        leg.state = LegState.CHASING
        leg.chase_start_time = current_time
        leg.last_chase_step_time = current_time
    
    def _process_chase(self, state: StrategyState, tick: QuoteTick):
        """Process chase logic - raise bids over time."""
        cfg = self.config
        
        # Determine which leg is filled and which is chasing
        if state.up_leg.state == LegState.FILLED and state.down_leg.state == LegState.CHASING:
            filled_leg = state.up_leg
            chasing_leg = state.down_leg
            chasing_ask = tick.down_ask
        elif state.down_leg.state == LegState.FILLED and state.up_leg.state == LegState.CHASING:
            filled_leg = state.down_leg
            chasing_leg = state.up_leg
            chasing_ask = tick.up_ask
        else:
            return  # No chase in progress
        
        # Check chase timeout
        chase_duration = state.current_time - chasing_leg.chase_start_time
        if chase_duration >= cfg.chase_timeout_secs:
            # Chase failed - unwind the filled leg
            self._unwind_leg(state, filled_leg, tick)
            state.reset_for_new_pair()
            return
        
        # Step up the bid if enough time has passed
        time_since_step = state.current_time - chasing_leg.last_chase_step_time
        if time_since_step >= cfg.chase_step_secs:
            # Calculate max bid based on pair cost cap
            max_bid = cfg.max_pair_cost_cents - filled_leg.fill_price
            
            # Raise bid by step, capped at max
            new_bid = min(
                chasing_leg.quote_bid + cfg.chase_step_cents,
                max_bid
            )
            
            if new_bid > chasing_leg.quote_bid:
                chasing_leg.quote_bid = new_bid
                chasing_leg.last_chase_step_time = state.current_time
    
    def _unwind_leg(self, state: StrategyState, leg: LegPosition, tick: QuoteTick):
        """Unwind a filled leg by selling at bid with slippage."""
        cfg = self.config
        
        # Get current bid for this side
        if leg.side == "UP":
            sell_price = max(0, tick.up_bid - cfg.slip_unwind_cents)
        else:
            sell_price = max(0, tick.down_bid - cfg.slip_unwind_cents)
        
        unwind = UnwindEvent(
            window_id=state.window_id,
            side=leg.side,
            buy_price=leg.fill_price,
            buy_time=leg.fill_time,
            sell_price=sell_price,
            sell_time=state.current_time
        )
        state.unwind_events.append(unwind)
    
    def _check_pair_complete(self, state: StrategyState, tick: QuoteTick):
        """Check if both legs are filled (pair complete)."""
        if state.up_leg.state == LegState.FILLED and state.down_leg.state == LegState.FILLED:
            # Determine order
            if state.up_leg.fill_time <= state.down_leg.fill_time:
                leg1 = state.up_leg
                leg2 = state.down_leg
            else:
                leg1 = state.down_leg
                leg2 = state.up_leg
            
            # Was this completed via chase?
            via_chase = (leg2.chase_start_time is not None)
            
            pair = CompletedPair(
                window_id=state.window_id,
                leg1_side=leg1.side,
                leg1_price=leg1.fill_price,
                leg1_time=leg1.fill_time,
                leg2_side=leg2.side,
                leg2_price=leg2.fill_price,
                leg2_time=leg2.fill_time,
                completed_via_chase=via_chase
            )
            
            # Apply fees
            pair.settled_pnl = self._compute_pair_pnl(pair)
            
            state.completed_pairs.append(pair)
            
            # Reset for next pair
            state.reset_for_new_pair()
    
    def _compute_pair_pnl(self, pair: CompletedPair) -> float:
        """Compute PnL for a completed pair including fees."""
        cfg = self.config
        
        # Gross edge (settlement returns 100c)
        gross_edge = pair.edge_cents / 100.0  # As fraction
        
        # Fee deductions (each leg is a buy)
        fee_cost = (pair.leg1_price + pair.leg2_price) * cfg.fee_bps_maker / 10000.0
        
        # Rebate (if any)
        rebate = (pair.leg1_price + pair.leg2_price) * cfg.maker_rebate_bps / 10000.0
        
        net_pnl = (pair.edge_cents / 100.0) - fee_cost + rebate
        return net_pnl
    
    def _finalize_window(self, state: StrategyState, window: WindowData):
        """Handle end-of-window cleanup.
        
        If we have a partial position (one leg filled), we must unwind.
        """
        if not window.ticks:
            return
        
        final_tick = window.ticks[-1]
        
        # Check for dangling positions
        if state.up_leg.state == LegState.FILLED and state.down_leg.state != LegState.FILLED:
            self._unwind_leg(state, state.up_leg, final_tick)
            state.reset_for_new_pair()
        elif state.down_leg.state == LegState.FILLED and state.up_leg.state != LegState.FILLED:
            self._unwind_leg(state, state.down_leg, final_tick)
            state.reset_for_new_pair()


