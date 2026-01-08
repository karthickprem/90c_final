"""
Strategy V3 State Machine

V3 Improvements over V2:
1. Pre-arm/maker-snipe entry (watch 86c, post limit at 90)
2. Probe-then-confirm sizing (0.5% probe, scale after gates)
3. Reversal hazard score (drawdown, crosses, slope, vol, spread, opp_accel)
4. Execution-aware exits (time-stop, opp-kill, TP ladder)
5. Regime filter (skip bad windows early)

All triggers/gates use ASK (buyable price).
All exits use BID (sellable price).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum, auto
from typing import List, Optional, Dict, Any, Tuple
import time

from .config import StrategyConfig


class State(Enum):
    """V3 Strategy state machine states."""
    IDLE = auto()           # Waiting for pre-arm or first-touch
    PRE_ARM = auto()        # Watching for entry opportunity (ask>=86 sustained)
    OBSERVE_10S = auto()    # Collecting validation window samples
    ENTRY_PENDING = auto()  # Waiting for limit fill
    IN_POSITION_PROBE = auto()   # Probe position (0.5% size)
    IN_POSITION_FULL = auto()    # Full position after scale-up
    DONE = auto()           # Lockout until next window


@dataclass
class Tick:
    """
    A single price sample with full bid/ask data.
    
    For realistic paper trading:
    - Use ASK for entry trigger/fill (price you pay to BUY)
    - Use BID for exit/sell (price you receive when SELL)
    """
    ts: float
    
    # UP side prices (cents)
    up_bid: int = 0
    up_ask: int = 0
    up_cents: int = 0  # Midpoint (for logging)
    
    # DOWN side prices (cents)
    down_bid: int = 0
    down_ask: int = 0
    down_cents: int = 0
    
    # Metadata
    is_synthetic: bool = False
    
    @property
    def up_spread(self) -> int:
        return self.up_ask - self.up_bid
    
    @property
    def down_spread(self) -> int:
        return self.down_ask - self.down_bid
    
    def is_valid(self) -> bool:
        return 0 <= self.up_ask <= 100 and 0 <= self.down_ask <= 100
    
    def get_buy_price(self, side: str) -> int:
        return self.up_ask if side == 'UP' else self.down_ask
    
    def get_sell_price(self, side: str) -> int:
        return self.up_bid if side == 'UP' else self.down_bid
    
    def get_spread(self, side: str) -> int:
        return self.up_spread if side == 'UP' else self.down_spread


@dataclass 
class HazardScore:
    """Reversal hazard score computed during validation window."""
    drawdown_from_high: int = 0      # Max drop from peak (cents)
    cross_count_90: int = 0          # How many times it crosses 90
    slope: float = 0.0               # Net change / seconds (c/s)
    volatility: float = 0.0          # Mean absolute delta
    avg_spread: float = 0.0          # Average spread at entry side
    opp_acceleration: float = 0.0    # Opposite ask increase rate
    
    def is_clean_push(self, config: 'V3Config') -> bool:
        """Check if this is a clean push (low reversal hazard)."""
        return (
            self.drawdown_from_high <= config.max_drawdown and
            self.cross_count_90 <= config.max_crosses and
            self.slope >= config.min_slope and
            self.avg_spread <= config.max_spread and
            self.opp_acceleration <= config.max_opp_accel
        )


@dataclass
class V3Config:
    """V3 Strategy configuration."""
    
    # Pre-arm / Maker-snipe
    pre_arm_threshold: int = 86      # Start watching when ask >= this
    pre_arm_persist_secs: float = 5.0  # Must persist for this long
    limit_entry_price: int = 90      # Post resting limit at this price
    
    # Trigger (fallback if no pre-arm fill)
    trigger_threshold: int = 90
    
    # SPIKE validation (chosen side only, 10s window)
    spike_min: int = 88
    spike_max: int = 93
    validation_secs: float = 10.0
    
    # JUMP gate (both sides)
    big_jump: int = 8
    mid_jump: int = 3
    max_mid_count: int = 2
    
    # Hazard score thresholds
    max_drawdown: int = 2            # Max cents drop from high
    max_crosses: int = 1             # Max times crossing 90
    min_slope: float = 0.25          # Min c/s upward
    max_spread: float = 3.0          # Max spread in cents
    max_opp_accel: float = 1.0       # Max opp acceleration (c/s)
    
    # Execution
    p_max: int = 93
    fill_timeout_secs: float = 2.0
    slip_entry: int = 1
    slip_exit: int = 1
    
    # Probe sizing
    probe_f: float = 0.005           # 0.5% bankroll for probe
    full_f: float = 0.02             # 2% bankroll for full
    
    # Exits - TP Ladder (bid-based)
    tp_levels: List[Tuple[int, float]] = field(default_factory=lambda: [
        (97, 0.50),  # Exit 50% at 97c
        (98, 0.25),  # Exit 25% at 98c
        (99, 0.25),  # Exit 25% at 99c
    ])
    
    # Exits - SL
    sl: int = 86
    
    # Exits - Time stop
    time_stop_secs: float = 30.0      # Exit if not >=95 within this time
    time_stop_target: int = 95        # Must reach this bid level
    
    # Exits - Opp kill switch
    opp_kill_threshold: int = 30      # Exit if opp ask >= this
    opp_kill_within_secs: float = 20.0  # Within this time after entry
    
    # Regime filter (skip window if)
    regime_first_ask_max: int = 97    # Skip if first tick ask >= this
    regime_max_spread: int = 3        # Skip if spread > this around trigger


@dataclass
class WindowContextV3:
    """Tracks state for a single 15-min window (V3)."""
    window_id: str
    window_start: float
    window_end: float
    
    # All ticks
    ticks: List[Tick] = field(default_factory=list)
    
    # Pre-arm state
    pre_arm_side: Optional[str] = None
    pre_arm_start_ts: Optional[float] = None
    pre_arm_price: Optional[int] = None
    
    # Trigger info
    trigger_ts: Optional[float] = None
    trigger_side: Optional[str] = None
    trigger_price: Optional[int] = None
    trigger_spread: int = 0
    
    # Validation window (10s)
    validation_ticks: List[Tick] = field(default_factory=list)
    spike_min_side: Optional[int] = None
    spike_max_side: Optional[int] = None
    spike_high_side: Optional[int] = None  # Track highest for drawdown
    jump_max_abs_delta: int = 0
    jump_mid_count: int = 0
    
    # Hazard metrics
    hazard: HazardScore = field(default_factory=HazardScore)
    hazard_passed: bool = False
    
    # Entry
    entry_type: Optional[str] = None  # 'MAKER_SNIPE' or 'TAKER'
    entry_submitted_ts: Optional[float] = None
    entry_fill_ts: Optional[float] = None
    entry_fill_price: Optional[int] = None
    entry_slip: int = 0
    
    # Position (supports partial exits)
    probe_shares: float = 0.0
    probe_cost: float = 0.0
    full_shares: float = 0.0
    full_cost: float = 0.0
    remaining_shares: float = 0.0
    total_cost: float = 0.0
    
    # TP ladder tracking
    tp_exits: List[Dict] = field(default_factory=list)
    
    # Exit
    exit_reason: Optional[str] = None
    exit_ts: Optional[float] = None
    exit_price: Optional[int] = None
    exit_slip: int = 0
    
    # P&L
    realized_pnl_invested: float = 0.0
    realized_pnl_dollars: float = 0.0
    total_proceeds: float = 0.0
    
    # Settlement
    settle_winner: Optional[str] = None
    
    # Skip reason (for stats)
    skip_reason: Optional[str] = None


class StrategyV3:
    """
    V3 Strategy State Machine.
    
    Key improvements:
    1. Pre-arm at 86c with resting limit at 90c
    2. Probe size (0.5%) then scale to full (2%) after hazard gates
    3. Hazard score with multiple features
    4. TP ladder exits at 97/98/99
    5. Time-stop and opp-kill safety exits
    6. Regime filter to skip bad windows
    """
    
    def __init__(self, config: V3Config):
        self.config = config
        self.state = State.IDLE
        self.context: Optional[WindowContextV3] = None
    
    def start_window(self, window_id: str, start_ts: float, end_ts: float) -> None:
        """Initialize for a new window."""
        self.state = State.IDLE
        self.context = WindowContextV3(
            window_id=window_id,
            window_start=start_ts,
            window_end=end_ts,
        )
    
    def process_tick(self, tick: Tick, bankroll: float) -> Dict[str, Any]:
        """Process a new tick and return status."""
        if not tick.is_valid():
            return {'state': self.state.name, 'trade_complete': False}
        
        if self.context is None:
            return {'state': 'NO_CONTEXT', 'trade_complete': False}
        
        # Add to window ticks
        self.context.ticks.append(tick)
        
        # Check regime filter on first tick
        if len(self.context.ticks) == 1:
            regime_result = self._check_regime_filter(tick)
            if regime_result:
                return regime_result
        
        result = {'state': self.state.name, 'trade_complete': False}
        
        if self.state == State.IDLE:
            result = self._handle_idle(tick, bankroll)
        
        elif self.state == State.PRE_ARM:
            result = self._handle_pre_arm(tick, bankroll)
        
        elif self.state == State.OBSERVE_10S:
            result = self._handle_observe(tick, bankroll)
        
        elif self.state == State.ENTRY_PENDING:
            result = self._handle_entry_pending(tick, bankroll)
        
        elif self.state == State.IN_POSITION_PROBE:
            result = self._handle_in_position_probe(tick, bankroll)
        
        elif self.state == State.IN_POSITION_FULL:
            result = self._handle_in_position_full(tick)
        
        elif self.state == State.DONE:
            result = {'state': 'DONE', 'trade_complete': True}
        
        return result
    
    def _check_regime_filter(self, tick: Tick) -> Optional[Dict[str, Any]]:
        """Check regime filter - skip bad windows early."""
        # Skip if first tick has ask >= 97 (already decided)
        up_ask = tick.up_ask
        down_ask = tick.down_ask
        
        if up_ask >= self.config.regime_first_ask_max or down_ask >= self.config.regime_first_ask_max:
            self.context.skip_reason = 'REGIME_ALREADY_DECIDED'
            self.context.exit_reason = 'REGIME_ALREADY_DECIDED'
            self.state = State.DONE
            return {'state': 'DONE', 'trade_complete': True, 'reason': 'REGIME_ALREADY_DECIDED'}
        
        return None
    
    def _handle_idle(self, tick: Tick, bankroll: float) -> Dict[str, Any]:
        """Look for pre-arm opportunity or direct trigger."""
        # Check for pre-arm conditions (ask >= 86)
        up_ask = tick.up_ask
        down_ask = tick.down_ask
        
        # Check if already at trigger (skip pre-arm)
        up_triggered = up_ask >= self.config.trigger_threshold
        down_triggered = down_ask >= self.config.trigger_threshold
        
        if up_triggered and down_triggered:
            # TIE - skip
            self.context.skip_reason = 'TIE_SKIP'
            self.context.exit_reason = 'TIE_SKIP'
            self.state = State.DONE
            return {'state': 'DONE', 'trade_complete': True, 'reason': 'TIE_SKIP'}
        
        if up_triggered:
            return self._direct_trigger('UP', tick, bankroll)
        
        if down_triggered:
            return self._direct_trigger('DOWN', tick, bankroll)
        
        # Check for pre-arm (ask >= 86, < 90)
        up_pre_arm = self.config.pre_arm_threshold <= up_ask < self.config.trigger_threshold
        down_pre_arm = self.config.pre_arm_threshold <= down_ask < self.config.trigger_threshold
        
        if up_pre_arm and not down_pre_arm:
            self.context.pre_arm_side = 'UP'
            self.context.pre_arm_start_ts = tick.ts
            self.context.pre_arm_price = up_ask
            self.state = State.PRE_ARM
            return {'state': 'PRE_ARM', 'trade_complete': False, 'pre_arm': 'UP'}
        
        if down_pre_arm and not up_pre_arm:
            self.context.pre_arm_side = 'DOWN'
            self.context.pre_arm_start_ts = tick.ts
            self.context.pre_arm_price = down_ask
            self.state = State.PRE_ARM
            return {'state': 'PRE_ARM', 'trade_complete': False, 'pre_arm': 'DOWN'}
        
        return {'state': 'IDLE', 'trade_complete': False}
    
    def _handle_pre_arm(self, tick: Tick, bankroll: float) -> Dict[str, Any]:
        """Watch pre-arm condition for persistence."""
        side = self.context.pre_arm_side
        side_ask = tick.get_buy_price(side)
        opp_side = 'DOWN' if side == 'UP' else 'UP'
        opp_ask = tick.get_buy_price(opp_side)
        
        # Check if pre-arm condition broken
        if side_ask < self.config.pre_arm_threshold:
            # Price dropped, cancel pre-arm
            self.context.pre_arm_side = None
            self.context.pre_arm_start_ts = None
            self.state = State.IDLE
            return {'state': 'IDLE', 'trade_complete': False, 'pre_arm_cancelled': 'price_drop'}
        
        # Check if opposite is rising (hazard)
        if opp_ask >= self.config.pre_arm_threshold:
            # Cancel pre-arm due to opposite hazard
            self.context.pre_arm_side = None
            self.context.pre_arm_start_ts = None
            self.state = State.IDLE
            return {'state': 'IDLE', 'trade_complete': False, 'pre_arm_cancelled': 'opp_hazard'}
        
        # Check if trigger reached
        if side_ask >= self.config.trigger_threshold:
            return self._maker_snipe_trigger(side, tick, bankroll)
        
        # Check if pre-arm persisted long enough
        elapsed = tick.ts - self.context.pre_arm_start_ts
        if elapsed >= self.config.pre_arm_persist_secs:
            # Pre-arm confirmed - post resting limit
            return self._post_maker_limit(side, tick, bankroll)
        
        return {'state': 'PRE_ARM', 'trade_complete': False, 'elapsed': elapsed}
    
    def _post_maker_limit(self, side: str, tick: Tick, bankroll: float) -> Dict[str, Any]:
        """Post a resting limit order at limit_entry_price."""
        # In paper mode, we simulate posting a limit at 90c
        # The order will fill when ask reaches/crosses 90
        self.context.trigger_side = side
        self.context.trigger_ts = tick.ts
        self.context.trigger_price = self.config.limit_entry_price
        self.context.trigger_spread = tick.get_spread(side)
        self.context.entry_type = 'MAKER_SNIPE'
        self.context.entry_submitted_ts = tick.ts
        self.state = State.ENTRY_PENDING
        
        return {
            'state': 'ENTRY_PENDING', 
            'trade_complete': False, 
            'maker_limit_posted': self.config.limit_entry_price
        }
    
    def _maker_snipe_trigger(self, side: str, tick: Tick, bankroll: float) -> Dict[str, Any]:
        """Trigger from pre-arm state (maker-snipe)."""
        self.context.trigger_side = side
        self.context.trigger_ts = tick.ts
        self.context.trigger_price = tick.get_buy_price(side)
        self.context.trigger_spread = tick.get_spread(side)
        self.context.entry_type = 'MAKER_SNIPE'
        
        # Start validation window
        self.state = State.OBSERVE_10S
        return {'state': 'OBSERVE_10S', 'trade_complete': False, 'triggered': True, 'type': 'MAKER_SNIPE'}
    
    def _direct_trigger(self, side: str, tick: Tick, bankroll: float) -> Dict[str, Any]:
        """Direct trigger (no pre-arm)."""
        self.context.trigger_side = side
        self.context.trigger_ts = tick.ts
        self.context.trigger_price = tick.get_buy_price(side)
        self.context.trigger_spread = tick.get_spread(side)
        self.context.entry_type = 'TAKER'
        
        # Check spread regime filter
        if self.context.trigger_spread > self.config.regime_max_spread:
            self.context.skip_reason = 'REGIME_WIDE_SPREAD'
            self.context.exit_reason = 'REGIME_WIDE_SPREAD'
            self.state = State.DONE
            return {'state': 'DONE', 'trade_complete': True, 'reason': 'REGIME_WIDE_SPREAD'}
        
        # Start validation window
        self.state = State.OBSERVE_10S
        return {'state': 'OBSERVE_10S', 'trade_complete': False, 'triggered': True, 'type': 'TAKER'}
    
    def _handle_observe(self, tick: Tick, bankroll: float) -> Dict[str, Any]:
        """Collect validation samples and compute hazard score."""
        elapsed = tick.ts - self.context.trigger_ts
        
        self.context.validation_ticks.append(tick)
        
        side = self.context.trigger_side
        side_ask = tick.get_buy_price(side)
        opp_side = 'DOWN' if side == 'UP' else 'UP'
        opp_ask = tick.get_buy_price(opp_side)
        
        # Update SPIKE metrics
        if self.context.spike_min_side is None:
            self.context.spike_min_side = side_ask
            self.context.spike_max_side = side_ask
            self.context.spike_high_side = side_ask
        else:
            self.context.spike_min_side = min(self.context.spike_min_side, side_ask)
            self.context.spike_max_side = max(self.context.spike_max_side, side_ask)
            self.context.spike_high_side = max(self.context.spike_high_side, side_ask)
        
        # Update JUMP metrics
        if len(self.context.validation_ticks) >= 2:
            prev = self.context.validation_ticks[-2]
            delta_up = abs(tick.up_ask - prev.up_ask)
            delta_down = abs(tick.down_ask - prev.down_ask)
            max_delta = max(delta_up, delta_down)
            
            self.context.jump_max_abs_delta = max(self.context.jump_max_abs_delta, max_delta)
            
            if max_delta >= self.config.mid_jump:
                self.context.jump_mid_count += 1
        
        # Check if validation window complete
        if elapsed >= self.config.validation_secs:
            return self._complete_validation(tick, bankroll)
        
        return {'state': 'OBSERVE_10S', 'trade_complete': False, 'elapsed': elapsed}
    
    def _complete_validation(self, tick: Tick, bankroll: float) -> Dict[str, Any]:
        """Check all gates and compute hazard score."""
        
        # SPIKE check
        spike_ok = (
            self.context.spike_min_side >= self.config.spike_min and
            self.context.spike_max_side >= self.config.spike_max
        )
        
        if not spike_ok:
            self.context.skip_reason = 'SPIKE_FAIL'
            self.context.exit_reason = 'SPIKE_FAIL'
            self.state = State.DONE
            return {'state': 'DONE', 'trade_complete': True, 'reason': 'SPIKE_FAIL'}
        
        # JUMP check
        jump_ok = (
            self.context.jump_max_abs_delta < self.config.big_jump and
            self.context.jump_mid_count < self.config.max_mid_count
        )
        
        if not jump_ok:
            self.context.skip_reason = 'JUMP_FAIL'
            self.context.exit_reason = 'JUMP_FAIL'
            self.state = State.DONE
            return {'state': 'DONE', 'trade_complete': True, 'reason': 'JUMP_FAIL'}
        
        # Compute hazard score
        self._compute_hazard_score()
        
        # Submit entry order
        self.context.entry_submitted_ts = tick.ts
        self.state = State.ENTRY_PENDING
        
        return self._try_probe_fill(tick, bankroll)
    
    def _compute_hazard_score(self) -> None:
        """Compute reversal hazard score from validation window."""
        ticks = self.context.validation_ticks
        if not ticks:
            return
        
        side = self.context.trigger_side
        
        # Drawdown from high
        high = self.context.spike_high_side
        current = ticks[-1].get_buy_price(side)
        self.context.hazard.drawdown_from_high = max(0, high - current)
        
        # Cross count (times crossing 90)
        cross_count = 0
        for i in range(1, len(ticks)):
            prev = ticks[i-1].get_buy_price(side)
            curr = ticks[i].get_buy_price(side)
            if (prev < 90 <= curr) or (curr < 90 <= prev):
                cross_count += 1
        self.context.hazard.cross_count_90 = cross_count
        
        # Slope (net change / time)
        if len(ticks) >= 2:
            first = ticks[0].get_buy_price(side)
            last = ticks[-1].get_buy_price(side)
            time_span = ticks[-1].ts - ticks[0].ts
            if time_span > 0:
                self.context.hazard.slope = (last - first) / time_span
        
        # Volatility (mean absolute delta)
        deltas = []
        for i in range(1, len(ticks)):
            delta = abs(ticks[i].get_buy_price(side) - ticks[i-1].get_buy_price(side))
            deltas.append(delta)
        if deltas:
            self.context.hazard.volatility = sum(deltas) / len(deltas)
        
        # Average spread
        spreads = [t.get_spread(side) for t in ticks]
        if spreads:
            self.context.hazard.avg_spread = sum(spreads) / len(spreads)
        
        # Opposite acceleration
        opp_side = 'DOWN' if side == 'UP' else 'UP'
        if len(ticks) >= 2:
            opp_first = ticks[0].get_buy_price(opp_side)
            opp_last = ticks[-1].get_buy_price(opp_side)
            time_span = ticks[-1].ts - ticks[0].ts
            if time_span > 0:
                self.context.hazard.opp_acceleration = (opp_last - opp_first) / time_span
        
        # Check if clean push
        self.context.hazard_passed = self.context.hazard.is_clean_push(self.config)
    
    def _handle_entry_pending(self, tick: Tick, bankroll: float) -> Dict[str, Any]:
        """Wait for fill or timeout."""
        elapsed = tick.ts - self.context.entry_submitted_ts
        
        if elapsed >= self.config.fill_timeout_secs:
            self.context.skip_reason = 'FILL_TIMEOUT'
            self.context.exit_reason = 'FILL_TIMEOUT'
            self.state = State.DONE
            return {'state': 'DONE', 'trade_complete': True, 'reason': 'FILL_TIMEOUT'}
        
        return self._try_probe_fill(tick, bankroll)
    
    def _try_probe_fill(self, tick: Tick, bankroll: float) -> Dict[str, Any]:
        """Try to fill PROBE position (small size first)."""
        side = self.context.trigger_side
        side_ask = tick.get_buy_price(side)
        
        # Apply slippage
        fill_price = side_ask + self.config.slip_entry
        
        # Check p_max
        if side_ask > self.config.p_max:
            return {'state': 'ENTRY_PENDING', 'trade_complete': False, 'ask': side_ask}
        
        # Fill PROBE position
        self.context.entry_fill_ts = tick.ts
        self.context.entry_fill_price = min(fill_price, self.config.p_max)
        self.context.entry_slip = self.context.entry_fill_price - side_ask
        
        # Calculate PROBE position (0.5% bankroll)
        self.context.probe_cost = bankroll * self.config.probe_f
        self.context.probe_shares = self.context.probe_cost / (self.context.entry_fill_price / 100.0)
        self.context.remaining_shares = self.context.probe_shares
        self.context.total_cost = self.context.probe_cost
        
        self.state = State.IN_POSITION_PROBE
        
        return {
            'state': 'IN_POSITION_PROBE', 
            'trade_complete': False, 
            'entry': True, 
            'fill_price': self.context.entry_fill_price,
            'probe_shares': self.context.probe_shares,
        }
    
    def _handle_in_position_probe(self, tick: Tick, bankroll: float) -> Dict[str, Any]:
        """Monitor probe position, decide to scale up or exit."""
        side = self.context.trigger_side
        side_bid = tick.get_sell_price(side)
        opp_side = 'DOWN' if side == 'UP' else 'UP'
        opp_ask = tick.get_buy_price(opp_side)
        
        elapsed_since_entry = tick.ts - self.context.entry_fill_ts
        
        # Check exits first (before scaling)
        
        # SL check
        if side_bid <= self.config.sl:
            return self._exit_all('SL', max(0, side_bid - self.config.slip_exit), tick.ts)
        
        # Time-stop check
        if elapsed_since_entry >= self.config.time_stop_secs:
            if side_bid < self.config.time_stop_target:
                return self._exit_all('TIME_STOP', side_bid, tick.ts)
        
        # Opp-kill check
        if elapsed_since_entry <= self.config.opp_kill_within_secs:
            if opp_ask >= self.config.opp_kill_threshold:
                return self._exit_all('OPP_KILL', side_bid, tick.ts)
        
        # Check hazard score for scale-up decision
        if self.context.hazard_passed:
            # Scale up to full position
            additional_cost = bankroll * (self.config.full_f - self.config.probe_f)
            additional_shares = additional_cost / (self.context.entry_fill_price / 100.0)
            
            self.context.full_shares = additional_shares
            self.context.full_cost = additional_cost
            self.context.remaining_shares = self.context.probe_shares + additional_shares
            self.context.total_cost = self.context.probe_cost + additional_cost
            
            self.state = State.IN_POSITION_FULL
            
            return {
                'state': 'IN_POSITION_FULL',
                'trade_complete': False,
                'scaled_up': True,
                'total_shares': self.context.remaining_shares,
            }
        else:
            # Stay in probe - don't scale
            # Check TP ladder for probe position
            return self._check_tp_ladder(tick, side_bid)
    
    def _handle_in_position_full(self, tick: Tick) -> Dict[str, Any]:
        """Monitor full position for exits."""
        side = self.context.trigger_side
        side_bid = tick.get_sell_price(side)
        opp_side = 'DOWN' if side == 'UP' else 'UP'
        opp_ask = tick.get_buy_price(opp_side)
        
        elapsed_since_entry = tick.ts - self.context.entry_fill_ts
        
        # SL check
        if side_bid <= self.config.sl:
            return self._exit_all('SL', max(0, side_bid - self.config.slip_exit), tick.ts)
        
        # Time-stop check (after scale-up, more lenient)
        if elapsed_since_entry >= self.config.time_stop_secs * 2:  # Double timeout for full
            if side_bid < self.config.time_stop_target:
                return self._exit_all('TIME_STOP', side_bid, tick.ts)
        
        # Opp-kill check (still applies)
        if elapsed_since_entry <= self.config.opp_kill_within_secs * 1.5:
            if opp_ask >= self.config.opp_kill_threshold:
                return self._exit_all('OPP_KILL', side_bid, tick.ts)
        
        # TP ladder
        return self._check_tp_ladder(tick, side_bid)
    
    def _check_tp_ladder(self, tick: Tick, side_bid: int) -> Dict[str, Any]:
        """Check TP ladder for partial exits."""
        for tp_level, fraction in self.config.tp_levels:
            if side_bid >= tp_level:
                # Check if we already exited at this level
                already_exited = any(e['level'] == tp_level for e in self.context.tp_exits)
                if not already_exited and self.context.remaining_shares > 0:
                    # Partial exit
                    shares_to_exit = self.context.remaining_shares * fraction / (1 - sum(e['fraction'] for e in self.context.tp_exits))
                    shares_to_exit = min(shares_to_exit, self.context.remaining_shares)
                    
                    proceeds = shares_to_exit * (tp_level / 100.0)
                    self.context.total_proceeds += proceeds
                    self.context.remaining_shares -= shares_to_exit
                    
                    self.context.tp_exits.append({
                        'level': tp_level,
                        'fraction': fraction,
                        'shares': shares_to_exit,
                        'proceeds': proceeds,
                        'ts': tick.ts,
                    })
                    
                    # Check if fully exited
                    if self.context.remaining_shares <= 0.01:  # Near zero
                        return self._finalize_exit('TP_LADDER', tp_level, tick.ts)
                    
                    return {
                        'state': self.state.name,
                        'trade_complete': False,
                        'partial_exit': tp_level,
                        'shares_exited': shares_to_exit,
                        'remaining': self.context.remaining_shares,
                    }
        
        return {'state': self.state.name, 'trade_complete': False, 'bid': side_bid}
    
    def _exit_all(self, reason: str, exit_price: int, ts: float) -> Dict[str, Any]:
        """Exit all remaining shares."""
        proceeds = self.context.remaining_shares * (exit_price / 100.0)
        self.context.total_proceeds += proceeds
        self.context.remaining_shares = 0
        
        return self._finalize_exit(reason, exit_price, ts)
    
    def _finalize_exit(self, reason: str, exit_price: int, ts: float) -> Dict[str, Any]:
        """Finalize exit and compute P&L."""
        self.context.exit_reason = reason
        self.context.exit_price = exit_price
        self.context.exit_ts = ts
        
        # Compute P&L
        self.context.realized_pnl_dollars = self.context.total_proceeds - self.context.total_cost
        if self.context.total_cost > 0:
            self.context.realized_pnl_invested = self.context.realized_pnl_dollars / self.context.total_cost
        
        self.state = State.DONE
        
        return {
            'state': 'DONE',
            'trade_complete': True,
            'exit': True,
            'reason': reason,
            'exit_price': exit_price,
            'pnl_dollars': self.context.realized_pnl_dollars,
        }
    
    def force_settle(self, winner: Optional[str]) -> Dict[str, Any]:
        """Force settlement at window end."""
        if self.state not in [State.IN_POSITION_PROBE, State.IN_POSITION_FULL]:
            return {'state': self.state.name, 'trade_complete': True}
        
        self.context.settle_winner = winner
        
        # Settle remaining shares
        if winner == self.context.trigger_side:
            exit_price = 100
            reason = 'SETTLEMENT_WIN'
        else:
            exit_price = 0
            reason = 'SETTLEMENT_LOSS'
        
        proceeds = self.context.remaining_shares * (exit_price / 100.0)
        self.context.total_proceeds += proceeds
        self.context.remaining_shares = 0
        
        return self._finalize_exit(reason, exit_price, time.time())
    
    def get_trade_record(self) -> Dict[str, Any]:
        """Get complete trade record for logging."""
        if self.context is None:
            return {}
        
        c = self.context
        h = c.hazard
        
        return {
            'window_id': c.window_id,
            'trigger_side': c.trigger_side,
            'trigger_price': c.trigger_price,
            'trigger_spread': c.trigger_spread,
            'trigger_ts': c.trigger_ts,
            'entry_type': c.entry_type,
            'spike_min': c.spike_min_side,
            'spike_max': c.spike_max_side,
            'jump_max_delta': c.jump_max_abs_delta,
            'jump_mid_count': c.jump_mid_count,
            'hazard_drawdown': h.drawdown_from_high,
            'hazard_crosses': h.cross_count_90,
            'hazard_slope': h.slope,
            'hazard_volatility': h.volatility,
            'hazard_avg_spread': h.avg_spread,
            'hazard_opp_accel': h.opp_acceleration,
            'hazard_passed': c.hazard_passed,
            'entry_fill_price': c.entry_fill_price,
            'entry_slip': c.entry_slip,
            'probe_shares': c.probe_shares,
            'probe_cost': c.probe_cost,
            'full_shares': c.full_shares,
            'full_cost': c.full_cost,
            'total_cost': c.total_cost,
            'tp_exits': c.tp_exits,
            'total_proceeds': c.total_proceeds,
            'exit_reason': c.exit_reason,
            'exit_price': c.exit_price,
            'exit_slip': c.exit_slip,
            'pnl_invested': c.realized_pnl_invested,
            'pnl_dollars': c.realized_pnl_dollars,
            'settle_winner': c.settle_winner,
            'ticks_count': len(c.ticks),
            'skip_reason': c.skip_reason,
        }


