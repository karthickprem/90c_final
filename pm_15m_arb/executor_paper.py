"""
Paper Execution Engine with Legging Protection

Simulates order execution for paired full-set trades.
Implements realistic legging logic:
1. Submit both legs "near-simultaneously"
2. If one fills and other doesn't within timeout:
   - Cancel remaining
   - Try to complete missing leg up to max_leg_slippage
   - Else unwind filled leg at best bid

Key: This is paper trading - no real orders placed.
"""

import logging
import random
import time
import uuid
from typing import Optional, Tuple, Dict, List, Any
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum

from .config import ArbConfig, load_config
from .orderbook import OrderbookFetcher, TickData, OrderBookSnapshot
from .metrics import MetricsLogger
from .ledger import Ledger

logger = logging.getLogger(__name__)


class OrderStatus(Enum):
    """Order status."""
    PENDING = "PENDING"
    SUBMITTED = "SUBMITTED"
    FILLED = "FILLED"
    PARTIAL = "PARTIAL"
    CANCELLED = "CANCELLED"
    FAILED = "FAILED"


class LegStatus(Enum):
    """Paired leg execution status."""
    BOTH_FILLED = "BOTH_FILLED"
    YES_ONLY = "YES_ONLY"
    NO_ONLY = "NO_ONLY"
    BOTH_FAILED = "BOTH_FAILED"
    TIMEOUT = "TIMEOUT"
    UNWOUND = "UNWOUND"


@dataclass
class SimulatedOrder:
    """A simulated order."""
    order_id: str
    side: str  # "YES" or "NO"
    token_id: str
    price: float  # Limit price
    qty: float
    status: OrderStatus = OrderStatus.PENDING
    filled_qty: float = 0
    fill_price: float = 0
    slippage: float = 0
    submit_time: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    fill_time: Optional[datetime] = None


@dataclass
class LegResult:
    """Result of executing one leg."""
    order: SimulatedOrder
    filled: bool
    partial: bool = False
    fill_price: float = 0
    slippage: float = 0


@dataclass
class PairExecutionResult:
    """Result of executing a paired trade."""
    yes_order: SimulatedOrder
    no_order: SimulatedOrder
    leg_status: LegStatus
    
    # Fill details
    qty_filled: float = 0  # Matched pairs
    cost_yes: float = 0
    cost_no: float = 0
    total_cost: float = 0
    
    # Legging event details
    legging_occurred: bool = False
    unwind_loss: float = 0
    
    # Timing
    execution_time_ms: float = 0


class PaperExecutor:
    """
    Paper trading executor for paired full-set trades.
    
    Simulates realistic execution with:
    - Probabilistic fills based on depth/spread
    - Partial fills
    - Legging protection (timeout, unwind)
    - VWAP-based fill pricing
    """
    
    def __init__(self, config: ArbConfig = None,
                 orderbook: OrderbookFetcher = None,
                 metrics: MetricsLogger = None,
                 ledger: Ledger = None):
        self.config = config or load_config()
        self.orderbook = orderbook
        self.metrics = metrics
        self.ledger = ledger
        
        # Statistics
        self.pairs_attempted = 0
        self.pairs_filled = 0
        self.legging_events = 0
        self.total_unwind_loss = 0
        
        # For replay determinism
        self._random = random.Random(self.config.replay_seed)
    
    def set_seed(self, seed: int):
        """Set random seed for deterministic replay."""
        self._random = random.Random(seed)
    
    def _simulate_fill_probability(self, 
                                   target_qty: float,
                                   available_depth: float,
                                   spread: float) -> Tuple[float, float]:
        """
        Estimate probability and extent of fill.
        
        Returns: (fill_probability, fill_fraction)
        """
        # Depth factor: more depth = more likely to fill
        depth_ratio = available_depth / max(target_qty, 1)
        depth_prob = min(1.0, depth_ratio * 0.95)
        
        # Spread factor: tighter spread = better liquidity = higher fill prob
        spread_factor = max(0.6, 1.0 - (spread * 5))
        
        # Combined probability (high base - we're at market)
        fill_prob = 0.92 * depth_prob * spread_factor
        
        # Add small noise for realism
        noise = self._random.uniform(-0.03, 0.03)
        fill_prob = max(0.1, min(0.98, fill_prob + noise))
        
        # Fill fraction (for partial fills)
        fill_fraction = min(1.0, depth_ratio) * self._random.uniform(0.85, 1.0)
        
        return fill_prob, fill_fraction
    
    def _simulate_leg_fill(self, 
                           order: SimulatedOrder,
                           book: OrderBookSnapshot) -> LegResult:
        """
        Simulate filling one leg using orderbook data.
        """
        if not book or not book.best_ask:
            return LegResult(order=order, filled=False)
        
        # Calculate fill probability
        spread = book.spread or 0.02
        depth = book.best_ask.size
        
        fill_prob, fill_fraction = self._simulate_fill_probability(
            order.qty, depth, spread
        )
        
        # Roll for fill
        roll = self._random.random()
        
        if roll < fill_prob:
            # Use VWAP for fill price
            vwap_result = book.vwap_buy(order.qty)
            
            if vwap_result.can_fill:
                fill_price = vwap_result.vwap
                filled_qty = order.qty
                partial = False
            else:
                # Partial fill at worst available price
                fill_price = vwap_result.worst_price if vwap_result.filled_shares > 0 else book.best_ask.price
                filled_qty = vwap_result.filled_shares
                partial = True
            
            # Add small execution slippage
            execution_slippage = self._random.uniform(0, 0.001)
            fill_price = min(fill_price + execution_slippage, 1.0)
            
            slippage = fill_price - order.price
            
            order.status = OrderStatus.PARTIAL if partial else OrderStatus.FILLED
            order.filled_qty = filled_qty
            order.fill_price = fill_price
            order.slippage = slippage
            order.fill_time = datetime.now(timezone.utc)
            
            return LegResult(
                order=order,
                filled=True,
                partial=partial,
                fill_price=fill_price,
                slippage=slippage
            )
        else:
            # Fill failed
            order.status = OrderStatus.FAILED
            return LegResult(order=order, filled=False)
    
    def _unwind_leg(self, order: SimulatedOrder, book: OrderBookSnapshot) -> float:
        """
        Unwind a filled leg at bid price.
        
        Returns the loss from unwinding.
        """
        if not book or not book.best_bid:
            # Worst case: lose everything paid
            return order.filled_qty * order.fill_price
        
        # Use VWAP for sell
        vwap_result = book.vwap_sell(order.filled_qty)
        unwind_price = vwap_result.vwap if vwap_result.can_fill else book.best_bid.price
        
        # Loss = cost - proceeds
        cost = order.filled_qty * order.fill_price
        proceeds = order.filled_qty * unwind_price
        loss = cost - proceeds
        
        return max(0, loss)
    
    def _try_complete_missing_leg(self, 
                                  missing_side: str,
                                  qty: float,
                                  book: OrderBookSnapshot,
                                  original_price: float) -> Tuple[bool, float]:
        """
        Try to complete a missing leg within slippage tolerance.
        
        Returns (success, fill_price).
        """
        if not book or not book.best_ask:
            return False, 0
        
        # Check if current price is within tolerance
        current_price = book.best_ask.price
        slippage = current_price - original_price
        slippage_pct = slippage / original_price if original_price > 0 else 0
        
        if slippage_pct > self.config.max_leg_slippage:
            logger.debug(f"Cannot complete {missing_side}: slippage {slippage_pct:.2%} > max {self.config.max_leg_slippage:.2%}")
            return False, 0
        
        # Try to fill
        vwap = book.vwap_buy(qty)
        if vwap.can_fill:
            return True, vwap.vwap
        
        return False, 0
    
    def execute_paired_trade(self, 
                             tick: TickData,
                             qty: float,
                             yes_limit: float = None,
                             no_limit: float = None) -> PairExecutionResult:
        """
        Execute a paired YES + NO trade.
        
        This is the main entry point for the strategy engine.
        
        Args:
            tick: Current orderbook tick data
            qty: Number of shares per leg
            yes_limit: Limit price for YES (default: best ask + buffer)
            no_limit: Limit price for NO (default: best ask + buffer)
        
        Returns:
            PairExecutionResult with fill details
        """
        start_time = time.time()
        self.pairs_attempted += 1
        
        # Set limit prices if not provided
        buffer = self.config.slippage_buffer_per_leg
        if yes_limit is None:
            yes_limit = tick.ask_yes + buffer
        if no_limit is None:
            no_limit = tick.ask_no + buffer
        
        # Create orders
        yes_order = SimulatedOrder(
            order_id=str(uuid.uuid4())[:8],
            side="YES",
            token_id=tick.yes_book.token_id if tick.yes_book else "",
            price=yes_limit,
            qty=qty,
        )
        
        no_order = SimulatedOrder(
            order_id=str(uuid.uuid4())[:8],
            side="NO",
            token_id=tick.no_book.token_id if tick.no_book else "",
            price=no_limit,
            qty=qty,
        )
        
        # Log order submissions
        if self.metrics:
            self.metrics.log_order_submit(
                yes_order.order_id, tick.market_id, "YES",
                yes_order.token_id, yes_order.price, yes_order.qty
            )
            self.metrics.log_order_submit(
                no_order.order_id, tick.market_id, "NO",
                no_order.token_id, no_order.price, no_order.qty
            )
        
        # Simulate fills
        yes_result = self._simulate_leg_fill(yes_order, tick.yes_book)
        no_result = self._simulate_leg_fill(no_order, tick.no_book)
        
        # Log fills
        if self.metrics:
            if yes_result.filled:
                self.metrics.log_fill(
                    yes_order.order_id, tick.market_id, "YES",
                    yes_order.filled_qty, yes_order.fill_price,
                    yes_order.slippage, yes_result.partial
                )
            if no_result.filled:
                self.metrics.log_fill(
                    no_order.order_id, tick.market_id, "NO",
                    no_order.filled_qty, no_order.fill_price,
                    no_order.slippage, no_result.partial
                )
        
        # Determine outcome
        yes_filled = yes_result.filled
        no_filled = no_result.filled
        
        legging_occurred = False
        unwind_loss = 0
        
        if yes_filled and no_filled:
            # SUCCESS: Both legs filled
            leg_status = LegStatus.BOTH_FILLED
            
            # Calculate matched quantity (min of both sides)
            qty_filled = min(yes_order.filled_qty, no_order.filled_qty)
            cost_yes = qty_filled * yes_order.fill_price
            cost_no = qty_filled * no_order.fill_price
            
            self.pairs_filled += 1
            
            logger.debug(
                f"BOTH FILLED: YES@{yes_order.fill_price:.4f} NO@{no_order.fill_price:.4f} "
                f"qty={qty_filled:.2f} cost={cost_yes + cost_no:.4f}"
            )
        
        elif yes_filled and not no_filled:
            # ONE LEG: YES filled, NO failed
            legging_occurred = True
            self.legging_events += 1
            
            # Try to complete missing leg
            success, fill_price = self._try_complete_missing_leg(
                "NO", yes_order.filled_qty, tick.no_book, tick.ask_no
            )
            
            if success:
                # Completed at higher price
                leg_status = LegStatus.BOTH_FILLED
                qty_filled = yes_order.filled_qty
                cost_yes = qty_filled * yes_order.fill_price
                cost_no = qty_filled * fill_price
                no_order.status = OrderStatus.FILLED
                no_order.filled_qty = qty_filled
                no_order.fill_price = fill_price
                
                if self.metrics:
                    self.metrics.log_leg_event(
                        tick.market_id, tick.window_id, "COMPLETE_MISSING",
                        "YES", "retry", details={"fill_price": fill_price}
                    )
                
                logger.debug(f"Completed missing NO leg at {fill_price:.4f}")
            else:
                # Must unwind YES
                leg_status = LegStatus.UNWOUND
                qty_filled = 0
                cost_yes = 0
                cost_no = 0
                
                unwind_loss = self._unwind_leg(yes_order, tick.yes_book)
                self.total_unwind_loss += unwind_loss
                
                if self.metrics:
                    self.metrics.log_leg_event(
                        tick.market_id, tick.window_id, "UNWIND",
                        "YES", "unwind", loss=unwind_loss
                    )
                
                logger.warning(f"Unwound YES leg, loss=${unwind_loss:.4f}")
        
        elif no_filled and not yes_filled:
            # ONE LEG: NO filled, YES failed
            legging_occurred = True
            self.legging_events += 1
            
            # Try to complete missing leg
            success, fill_price = self._try_complete_missing_leg(
                "YES", no_order.filled_qty, tick.yes_book, tick.ask_yes
            )
            
            if success:
                leg_status = LegStatus.BOTH_FILLED
                qty_filled = no_order.filled_qty
                cost_yes = qty_filled * fill_price
                cost_no = qty_filled * no_order.fill_price
                yes_order.status = OrderStatus.FILLED
                yes_order.filled_qty = qty_filled
                yes_order.fill_price = fill_price
                
                if self.metrics:
                    self.metrics.log_leg_event(
                        tick.market_id, tick.window_id, "COMPLETE_MISSING",
                        "NO", "retry", details={"fill_price": fill_price}
                    )
                
                logger.debug(f"Completed missing YES leg at {fill_price:.4f}")
            else:
                leg_status = LegStatus.UNWOUND
                qty_filled = 0
                cost_yes = 0
                cost_no = 0
                
                unwind_loss = self._unwind_leg(no_order, tick.no_book)
                self.total_unwind_loss += unwind_loss
                
                if self.metrics:
                    self.metrics.log_leg_event(
                        tick.market_id, tick.window_id, "UNWIND",
                        "NO", "unwind", loss=unwind_loss
                    )
                
                logger.warning(f"Unwound NO leg, loss=${unwind_loss:.4f}")
        
        else:
            # BOTH FAILED: No fills
            leg_status = LegStatus.BOTH_FAILED
            qty_filled = 0
            cost_yes = 0
            cost_no = 0
            
            logger.debug("Both legs failed to fill")
        
        execution_time_ms = (time.time() - start_time) * 1000
        
        return PairExecutionResult(
            yes_order=yes_order,
            no_order=no_order,
            leg_status=leg_status,
            qty_filled=qty_filled,
            cost_yes=cost_yes,
            cost_no=cost_no,
            total_cost=cost_yes + cost_no,
            legging_occurred=legging_occurred,
            unwind_loss=unwind_loss,
            execution_time_ms=execution_time_ms,
        )
    
    def get_stats(self) -> Dict[str, Any]:
        """Get executor statistics."""
        fill_rate = self.pairs_filled / max(1, self.pairs_attempted) * 100
        
        return {
            "pairs_attempted": self.pairs_attempted,
            "pairs_filled": self.pairs_filled,
            "fill_rate_pct": round(fill_rate, 2),
            "legging_events": self.legging_events,
            "total_unwind_loss": round(self.total_unwind_loss, 4),
            "avg_unwind_loss": round(self.total_unwind_loss / max(1, self.legging_events), 4),
        }


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    
    print("\n=== Paper Executor Module ===\n")
    
    # Create mock executor
    config = ArbConfig()
    executor = PaperExecutor(config)
    
    print(f"Max leg timeout: {config.max_leg_timeout_ms}ms")
    print(f"Max leg slippage: {config.max_leg_slippage:.2%}")
    print(f"Max unwind loss: {config.max_unwind_loss_pct:.2%}")
    
    print("\nExecutor ready. Use with strategy engine.")

