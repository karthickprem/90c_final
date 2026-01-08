"""
Strategy Engine v1 - Variant A (Paired Full-Set Arb)

Implements the core trading logic:
- Signal: PairCost = AskYES + AskNO + buffers <= 1 - min_edge
- Position: Track QtyY, QtyN, CostY, CostN
- Lock: SafeProfitNet = min(QtyY, QtyN) - (CostY + CostN) - buffers
- Stop: When SafeProfitNet >= target_profit OR near window end

Variant B (overlay) is behind feature flag and OFF by default.
"""

import logging
import time
from typing import Optional, Dict, Any, List
from dataclasses import dataclass, field
from datetime import datetime, timezone

from .config import ArbConfig, load_config
from .market_discovery import BTC15mMarket
from .orderbook import OrderbookFetcher, TickData
from .executor_paper import PaperExecutor, LegStatus
from .metrics import MetricsLogger
from .ledger import Ledger

logger = logging.getLogger(__name__)


@dataclass
class PositionState:
    """
    Current position state for a trading window.
    
    Core accounting:
    - QtyYES: Total YES shares held
    - QtyNO: Total NO shares held
    - CostYES: Total cost of YES shares
    - CostNO: Total cost of NO shares
    - SafeProfitNet: Guaranteed profit at settlement
    """
    qty_yes: float = 0
    qty_no: float = 0
    cost_yes: float = 0
    cost_no: float = 0
    
    # Trade counts
    trades_count: int = 0
    pairs_filled: int = 0
    legging_events: int = 0
    
    @property
    def total_cost(self) -> float:
        """Total cost of position."""
        return self.cost_yes + self.cost_no
    
    @property
    def min_qty(self) -> float:
        """Minimum of YES/NO quantities (guaranteed payout)."""
        return min(self.qty_yes, self.qty_no)
    
    @property
    def excess_qty(self) -> float:
        """Excess on one side (directional exposure)."""
        return abs(self.qty_yes - self.qty_no)
    
    @property
    def is_balanced(self) -> bool:
        """Position is balanced (no directional exposure)."""
        return abs(self.qty_yes - self.qty_no) < 0.01
    
    def safe_profit_net(self, buffers: float = 0) -> float:
        """
        Calculate SafeProfitNet.
        
        SafeProfitNet = min(QtyY, QtyN) * 1.0 - TotalCost - buffers
        
        This is the guaranteed profit at settlement.
        The min(QtyY, QtyN) shares will pay out $1 each.
        """
        redemption_value = self.min_qty * 1.0
        return redemption_value - self.total_cost - buffers
    
    def update_from_fill(self, side: str, qty: float, cost: float):
        """Update position after a fill."""
        if side == "YES":
            self.qty_yes += qty
            self.cost_yes += cost
        else:
            self.qty_no += qty
            self.cost_no += cost
        self.trades_count += 1
    
    def to_dict(self) -> Dict[str, Any]:
        """Convert to dictionary."""
        return {
            "qty_yes": self.qty_yes,
            "qty_no": self.qty_no,
            "cost_yes": self.cost_yes,
            "cost_no": self.cost_no,
            "total_cost": self.total_cost,
            "min_qty": self.min_qty,
            "safe_profit_net": self.safe_profit_net(),
            "trades_count": self.trades_count,
            "pairs_filled": self.pairs_filled,
            "legging_events": self.legging_events,
        }


@dataclass
class WindowResult:
    """Result of trading a single window."""
    window_id: str
    market_id: str
    
    # Final position state
    qty_yes: float = 0
    qty_no: float = 0
    cost_yes: float = 0
    cost_no: float = 0
    safe_profit_net: float = 0
    
    # Trade metrics
    trades_count: int = 0
    pairs_filled: int = 0
    legging_events: int = 0
    
    # Signals seen
    signals_seen: int = 0
    signals_taken: int = 0
    
    # Timing
    start_time: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    end_time: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    ticks_processed: int = 0
    
    # Stop reason
    stop_reason: str = ""
    
    def __repr__(self):
        return f"WindowResult({self.window_id}: PnL=${self.safe_profit_net:.4f}, trades={self.trades_count})"


class StrategyEngine:
    """
    Strategy engine implementing Variant A (paired full-set arb).
    
    Core loop per window:
    1. Poll orderbook at config.poll_interval_ms
    2. Check signal: pair_cost <= 1 - min_edge - buffers
    3. If signal and depth sufficient: execute paired trade
    4. Update position state
    5. Stop if SafeProfitNet >= target_profit or near end
    """
    
    def __init__(self, config: ArbConfig = None,
                 executor: PaperExecutor = None,
                 metrics: MetricsLogger = None,
                 ledger: Ledger = None):
        self.config = config or load_config()
        self.executor = executor
        self.metrics = metrics
        self.ledger = ledger
        
        # Current position
        self.position = PositionState()
        
        # Current window
        self.current_window: Optional[BTC15mMarket] = None
    
    def _check_signal(self, tick: TickData) -> tuple[bool, float, float, Optional[str]]:
        """
        Check if current tick presents a trading signal.
        
        Variant A rule:
        pair_cost = ask_yes + ask_no + buffers
        Signal if pair_cost <= 1 - min_edge
        
        Returns:
            (is_actionable, pair_cost, edge, reject_reason)
        """
        # Calculate pair cost with buffers
        raw_pair_cost = tick.ask_yes + tick.ask_no
        buffers = self.config.total_buffer_per_pair
        pair_cost = raw_pair_cost + buffers
        
        # Calculate edge
        edge = 1.0 - pair_cost
        
        # Check depth requirement
        min_depth = tick.min_depth
        
        # Check thresholds
        if edge < self.config.min_edge:
            return False, pair_cost, edge, f"Edge {edge:.4f} < min {self.config.min_edge:.4f}"
        
        if min_depth < self.config.min_depth_shares:
            return False, pair_cost, edge, f"Depth {min_depth:.0f} < min {self.config.min_depth_shares:.0f}"
        
        return True, pair_cost, edge, None
    
    def _calculate_trade_size(self, tick: TickData, edge: float) -> float:
        """
        Calculate appropriate trade size.
        
        Considers:
        - Order size from config
        - Available depth
        - Risk limits
        - Remaining notional for window
        """
        # Base size from config
        pair_cost = tick.sum_asks + self.config.total_buffer_per_pair
        base_shares = self.config.order_size_usd / max(pair_cost, 0.1)
        
        # Cap by available depth (use 90% to leave room)
        depth_shares = tick.min_depth * 0.9
        
        # Cap by remaining notional for window
        used_notional = self.position.total_cost
        remaining_notional = self.config.max_notional_per_window - used_notional
        notional_shares = remaining_notional / max(pair_cost, 0.1)
        
        # Take minimum
        shares = min(base_shares, depth_shares, notional_shares)
        
        return max(0, shares)
    
    def _should_stop_trading(self, market: BTC15mMarket) -> tuple[bool, str]:
        """
        Check if we should stop trading this window.
        
        Reasons:
        1. SafeProfitNet >= target_profit (goal achieved)
        2. Near window end (time cutoff)
        3. Risk limits hit
        """
        # Check target profit
        safe_profit = self.position.safe_profit_net(self.config.total_buffer_per_pair)
        if safe_profit >= self.config.target_profit:
            return True, f"Target profit reached: ${safe_profit:.4f}"
        
        # Check time cutoff
        seconds_remaining = market.seconds_remaining
        if seconds_remaining < self.config.stop_add_seconds_before_end:
            return True, f"Time cutoff: {seconds_remaining:.0f}s remaining"
        
        # Check max notional
        if self.position.total_cost >= self.config.max_notional_per_window:
            return True, f"Max notional reached: ${self.position.total_cost:.2f}"
        
        return False, ""
    
    def _check_overlay_b(self, tick: TickData) -> tuple[bool, str, float]:
        """
        Check Overlay B signal (min-side-only, strictly safe).
        
        Only enabled if config.enable_overlay_b = True.
        
        Rule:
        - Only add to the smaller side
        - Only if simulated SafeProfitNet strictly increases by min_improvement
        - Only if new SafeProfitNet stays >= 0 (if overlay_b_never_negative)
        
        Returns:
            (should_trade, side, qty)
        """
        if not self.config.enable_overlay_b:
            return False, "", 0
        
        # Determine smaller side
        if self.position.qty_yes < self.position.qty_no:
            smaller_side = "YES"
            smaller_qty = self.position.qty_yes
            smaller_cost = self.position.cost_yes
            ask_price = tick.ask_yes
        else:
            smaller_side = "NO"
            smaller_qty = self.position.qty_no
            smaller_cost = self.position.cost_no
            ask_price = tick.ask_no
        
        # Only trade if we have imbalance
        imbalance = abs(self.position.qty_yes - self.position.qty_no)
        if imbalance < 1:
            return False, "", 0
        
        # Calculate qty to add (balance the position)
        target_qty = min(imbalance, self.config.min_depth_shares * 0.5)
        
        # Simulate new position
        current_safe_profit = self.position.safe_profit_net(self.config.total_buffer_per_pair)
        
        new_qty = smaller_qty + target_qty
        new_cost = smaller_cost + target_qty * ask_price
        
        if smaller_side == "YES":
            new_min_qty = min(new_qty, self.position.qty_no)
            new_total_cost = new_cost + self.position.cost_no
        else:
            new_min_qty = min(self.position.qty_yes, new_qty)
            new_total_cost = self.position.cost_yes + new_cost
        
        new_safe_profit = new_min_qty * 1.0 - new_total_cost - self.config.total_buffer_per_pair
        
        improvement = new_safe_profit - current_safe_profit
        
        # Check improvement threshold
        if improvement < self.config.min_improvement_b:
            return False, "", 0
        
        # Check never-negative rule
        if self.config.overlay_b_never_negative and new_safe_profit < 0:
            return False, "", 0
        
        logger.debug(f"Overlay B: {smaller_side} +{target_qty:.2f}, improvement=${improvement:.4f}")
        return True, smaller_side, target_qty
    
    def trade_window(self, market: BTC15mMarket, 
                     orderbook_source=None) -> WindowResult:
        """
        Trade a single 15-minute window.
        
        Args:
            market: The BTC 15-min market to trade
            orderbook_source: OrderbookFetcher or Replayer
        
        Returns:
            WindowResult with final position and metrics
        """
        # Reset position for new window
        self.position = PositionState()
        self.current_window = market
        
        start_time = datetime.now(timezone.utc)
        ticks_processed = 0
        signals_seen = 0
        signals_taken = 0
        stop_reason = ""
        
        # Log window start
        if self.metrics:
            self.metrics.log_window_start(
                market.market_id,
                market.window_id,
                market.start_ts,
                market.end_ts,
                market.yes_token_id,
                market.no_token_id
            )
        
        logger.info(f"Trading window: {market.window_id}")
        
        # Main trading loop
        while True:
            # Check if we should stop
            should_stop, stop_reason = self._should_stop_trading(market)
            if should_stop:
                logger.info(f"Stopping: {stop_reason}")
                break
            
            # Fetch orderbook tick
            tick = None
            if orderbook_source:
                tick = orderbook_source.fetch_top_of_book(
                    market.yes_token_id,
                    market.no_token_id,
                    market.market_id,
                    market.window_id
                )
            
            if not tick:
                # No more ticks (replay) or fetch failed
                if hasattr(orderbook_source, 'has_more_ticks'):
                    if not orderbook_source.has_more_ticks():
                        stop_reason = "End of recorded data"
                        break
                time.sleep(self.config.poll_interval_seconds)
                continue
            
            ticks_processed += 1
            
            # Log tick
            if self.metrics:
                self.metrics.log_tick(
                    market.market_id,
                    market.window_id,
                    tick.ask_yes, tick.ask_yes_size,
                    tick.ask_no, tick.ask_no_size,
                    tick.bid_yes, tick.bid_no,
                )
            
            # Check Variant A signal
            is_actionable, pair_cost, edge, reject_reason = self._check_signal(tick)
            
            if self.metrics:
                self.metrics.log_signal(
                    market.market_id,
                    market.window_id,
                    pair_cost, edge,
                    is_actionable, reject_reason
                )
            
            if is_actionable:
                signals_seen += 1
                
                # Calculate trade size
                qty = self._calculate_trade_size(tick, edge)
                
                if qty >= 1:  # Minimum 1 share
                    # Execute paired trade
                    result = self.executor.execute_paired_trade(tick, qty)
                    
                    if result.leg_status == LegStatus.BOTH_FILLED:
                        # Update position
                        self.position.qty_yes += result.qty_filled
                        self.position.qty_no += result.qty_filled
                        self.position.cost_yes += result.cost_yes
                        self.position.cost_no += result.cost_no
                        self.position.pairs_filled += 1
                        signals_taken += 1
                        
                        logger.debug(
                            f"Pair filled: qty={result.qty_filled:.2f} cost=${result.total_cost:.4f} "
                            f"SafeProfit=${self.position.safe_profit_net():.4f}"
                        )
                    
                    if result.legging_occurred:
                        self.position.legging_events += 1
                    
                    self.position.trades_count += 1
                    
                    # Log position update
                    if self.metrics:
                        safe_profit = self.position.safe_profit_net(self.config.total_buffer_per_pair)
                        self.metrics.log_position_update(
                            market.market_id,
                            market.window_id,
                            self.position.qty_yes,
                            self.position.qty_no,
                            self.position.cost_yes,
                            self.position.cost_no,
                            safe_profit
                        )
            
            # Check Overlay B (if enabled and we're not in cutoff)
            if self.config.enable_overlay_b and market.seconds_remaining > self.config.stop_add_seconds_before_end:
                overlay_ok, side, overlay_qty = self._check_overlay_b(tick)
                
                if overlay_ok:
                    # Execute single-leg overlay (simplified - would need separate execution)
                    logger.debug(f"Overlay B signal: {side} +{overlay_qty:.2f} (not yet implemented)")
            
            # Wait for next poll (unless replaying)
            if not hasattr(orderbook_source, 'has_more_ticks'):
                time.sleep(self.config.poll_interval_seconds)
        
        # Calculate final SafeProfitNet
        final_safe_profit = self.position.safe_profit_net(self.config.total_buffer_per_pair)
        
        # Log window end
        if self.metrics:
            self.metrics.log_window_end(
                market.market_id,
                market.window_id,
                self.position.qty_yes,
                self.position.qty_no,
                self.position.cost_yes,
                self.position.cost_no,
                final_safe_profit,
                self.position.trades_count,
                self.position.legging_events
            )
        
        # Store in ledger
        if self.ledger:
            self.ledger.log_window(
                market.market_id,
                market.window_id,
                self.position.to_dict(),
                final_safe_profit
            )
        
        end_time = datetime.now(timezone.utc)
        
        result = WindowResult(
            window_id=market.window_id,
            market_id=market.market_id,
            qty_yes=self.position.qty_yes,
            qty_no=self.position.qty_no,
            cost_yes=self.position.cost_yes,
            cost_no=self.position.cost_no,
            safe_profit_net=final_safe_profit,
            trades_count=self.position.trades_count,
            pairs_filled=self.position.pairs_filled,
            legging_events=self.position.legging_events,
            signals_seen=signals_seen,
            signals_taken=signals_taken,
            start_time=start_time,
            end_time=end_time,
            ticks_processed=ticks_processed,
            stop_reason=stop_reason,
        )
        
        logger.info(
            f"Window complete: {result.window_id} | "
            f"PnL=${final_safe_profit:.4f} | "
            f"Trades={self.position.trades_count} | "
            f"Signals={signals_taken}/{signals_seen}"
        )
        
        return result
    
    def get_position_state(self) -> Dict[str, Any]:
        """Get current position state."""
        return self.position.to_dict()


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    
    print("\n=== Strategy Engine v1 (Variant A) ===\n")
    
    config = ArbConfig()
    config.print_summary()
    
    print("Strategy ready. Use with trade_window() method.")
    print("\nKey parameters:")
    print(f"  Min edge: {config.min_edge:.3f}")
    print(f"  Target profit: ${config.target_profit}")
    print(f"  Stop before end: {config.stop_add_seconds_before_end}s")
    print(f"  Overlay B: {'ENABLED' if config.enable_overlay_b else 'DISABLED'}")

