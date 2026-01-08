"""
Engine V2 - Properly Constrained Paper Trading

Key fixes from expert review:
1. Hedge feasibility gate: never buy cheap side without credible hedge path
2. Per-window inventory cap: max_risk_usd_per_window
3. Variant M (maker full-set): post bids on both sides, bid_up + bid_down <= 1 - edge
4. Conservative maker fill model: fill only on cross-through (ask <= bid)
5. Price sanity checks and token validation

Two strategies:
- Variant L (Legging with rescue): Take one side, must rescue-hedge within bounds
- Variant M (Maker full-set): Post bids on both sides for spread capture
"""

import logging
import time
import json
import random
from pathlib import Path
from datetime import datetime, timezone
from typing import Optional, List, Dict, Tuple
from dataclasses import dataclass, field
from enum import Enum

from .market_v2 import (
    MarketFetcher, 
    Window15Min, 
    OrderBookTick,
    get_current_window_slug,
)

logger = logging.getLogger(__name__)


class Side(Enum):
    UP = "up"
    DOWN = "down"


@dataclass
class MakerBid:
    """A resting maker bid."""
    side: Side
    price: float
    size: float
    posted_ts: float
    filled: bool = False
    filled_ts: float = 0.0
    fill_price: float = 0.0


@dataclass
class Position:
    """Position state with proper accounting."""
    q_up: float = 0.0
    q_down: float = 0.0
    cost_up: float = 0.0
    cost_down: float = 0.0
    
    @property
    def avg_up(self) -> float:
        return self.cost_up / self.q_up if self.q_up > 0 else 0
    
    @property
    def avg_down(self) -> float:
        return self.cost_down / self.q_down if self.q_down > 0 else 0
    
    @property
    def total_cost(self) -> float:
        return self.cost_up + self.cost_down
    
    @property
    def min_qty(self) -> float:
        return min(self.q_up, self.q_down)
    
    @property
    def hedged_qty(self) -> float:
        """Quantity that is fully hedged (matched on both sides)."""
        return self.min_qty
    
    @property
    def unhedged_qty(self) -> float:
        """Quantity exposed to directional risk."""
        return abs(self.q_up - self.q_down)
    
    @property
    def unhedged_exposure(self) -> float:
        """Dollar value at risk from unhedged position."""
        if self.q_up > self.q_down:
            # Excess Up: at risk if Down wins
            excess = self.q_up - self.q_down
            return excess * self.avg_up if self.avg_up > 0 else 0
        else:
            # Excess Down: at risk if Up wins
            excess = self.q_down - self.q_up
            return excess * self.avg_down if self.avg_down > 0 else 0
    
    @property
    def pair_cost(self) -> float:
        """Average cost per hedged pair."""
        if self.min_qty > 0:
            hedged_cost = (self.avg_up + self.avg_down)
            return hedged_cost
        return 0
    
    @property
    def guaranteed_profit(self) -> float:
        """Profit locked in from hedged portion."""
        if self.min_qty > 0:
            return self.min_qty * (1.0 - self.pair_cost)
        return 0
    
    @property
    def max_loss(self) -> float:
        """Maximum possible loss (unhedged exposure)."""
        return self.unhedged_exposure
    
    @property
    def is_hedged(self) -> bool:
        return self.q_up > 0 and self.q_down > 0
    
    @property
    def is_fully_hedged(self) -> bool:
        return self.q_up > 0 and self.q_up == self.q_down
    
    def record_fill(self, side: Side, shares: float, price: float):
        if side == Side.UP:
            self.q_up += shares
            self.cost_up += shares * price
        else:
            self.q_down += shares
            self.cost_down += shares * price
    
    def to_dict(self) -> dict:
        return {
            "q_up": self.q_up,
            "q_down": self.q_down,
            "cost_up": self.cost_up,
            "cost_down": self.cost_down,
            "avg_up": self.avg_up,
            "avg_down": self.avg_down,
            "pair_cost": self.pair_cost,
            "hedged_qty": self.hedged_qty,
            "unhedged_qty": self.unhedged_qty,
            "unhedged_exposure": self.unhedged_exposure,
            "guaranteed_profit": self.guaranteed_profit,
            "max_loss": self.max_loss,
        }


@dataclass
class WindowResult:
    """Result of trading a single window."""
    slug: str
    start_ts: int
    end_ts: int
    strategy: str  # "L" or "M"
    
    # Tracking
    ticks_seen: int = 0
    
    # Variant L metrics
    entry_attempts: int = 0
    entry_blocked_by_feasibility: int = 0
    entry_blocked_by_risk_cap: int = 0
    rescue_triggered: int = 0
    
    # Variant M metrics
    bids_posted: int = 0
    bids_filled_up: int = 0
    bids_filled_down: int = 0
    pairs_completed: int = 0
    
    # Final state
    final_position: Optional[Position] = None
    
    # Outcome
    hedged: bool = False
    achieved_pair_cost: float = 0.0
    guaranteed_profit: float = 0.0
    max_loss: float = 0.0
    
    def to_dict(self) -> dict:
        return {
            "slug": self.slug,
            "strategy": self.strategy,
            "ticks_seen": self.ticks_seen,
            "entry_attempts": self.entry_attempts,
            "entry_blocked_by_feasibility": self.entry_blocked_by_feasibility,
            "entry_blocked_by_risk_cap": self.entry_blocked_by_risk_cap,
            "rescue_triggered": self.rescue_triggered,
            "bids_posted": self.bids_posted,
            "bids_filled_up": self.bids_filled_up,
            "bids_filled_down": self.bids_filled_down,
            "pairs_completed": self.pairs_completed,
            "hedged": self.hedged,
            "achieved_pair_cost": self.achieved_pair_cost,
            "guaranteed_profit": self.guaranteed_profit,
            "max_loss": self.max_loss,
            "final_position": self.final_position.to_dict() if self.final_position else None,
        }


class EngineV2:
    """
    Properly constrained paper trading engine.
    
    Implements:
    - Variant L: Legging with rescue bounds
    - Variant M: Maker full-set spread capture
    """
    
    def __init__(
        self,
        # Strategy selection
        strategy: str = "M",  # "L" (legging) or "M" (maker)
        
        # Risk controls (Fix #2)
        max_risk_usd_per_window: float = 2.0,  # Max unhedged exposure
        max_unhedged_qty: float = 20.0,        # Max unhedged shares
        loss_cap: float = 1.0,                  # Max acceptable rescue loss
        
        # Hedge feasibility (Fix #1)
        min_edge: float = 0.01,                # Minimum edge for entry
        
        # Variant L params
        rescue_window_seconds: float = 120,    # Rescue if not hedged by this
        
        # Variant M params
        edge_target: float = 0.01,             # Target bid_up + bid_down <= 1 - edge
        bid_clip_size: float = 5.0,            # Shares per bid
        bid_improve_delta: float = 0.005,      # Improve bid by this when one fills
        
        # Timing
        stop_buffer_seconds: float = 60,       # Stop trading N sec before end
        
        # Output
        output_dir: str = "pm_results_v2",
    ):
        self.strategy = strategy
        self.max_risk_usd_per_window = max_risk_usd_per_window
        self.max_unhedged_qty = max_unhedged_qty
        self.loss_cap = loss_cap
        self.min_edge = min_edge
        self.rescue_window_seconds = rescue_window_seconds
        self.edge_target = edge_target
        self.bid_clip_size = bid_clip_size
        self.bid_improve_delta = bid_improve_delta
        self.stop_buffer_seconds = stop_buffer_seconds
        
        self.output_dir = Path(output_dir)
        self.output_dir.mkdir(exist_ok=True)
        
        self.fetcher = MarketFetcher()
        
        # Current state
        self.current_window: Optional[Window15Min] = None
        self.position: Optional[Position] = None
        self.result: Optional[WindowResult] = None
        
        # Variant M state
        self.bid_up: Optional[MakerBid] = None
        self.bid_down: Optional[MakerBid] = None
        
        # Variant L state
        self.first_leg_ts: float = 0.0  # When first leg was taken
        
        # All results
        self.results: List[WindowResult] = []
        
        # Logging
        self.log_file = None
        self._open_log()
    
    def _open_log(self):
        ts = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
        self.log_path = self.output_dir / f"trades_v2_{ts}.jsonl"
        self.log_file = open(self.log_path, "a")
    
    def _log(self, event_type: str, data: dict):
        event = {
            "ts": time.time(),
            "ts_iso": datetime.now(timezone.utc).isoformat(),
            "event": event_type,
            "window": self.current_window.slug if self.current_window else None,
            **data,
        }
        if self.log_file:
            self.log_file.write(json.dumps(event) + "\n")
            self.log_file.flush()
    
    def _switch_window(self, new_slug: str):
        """Handle window transition."""
        # Finalize previous window
        if self.result and self.position:
            self._finalize_result()
            self.results.append(self.result)
            self._log("WINDOW_END", self.result.to_dict())
        
        # Start new window
        self.current_window = self.fetcher.fetch_market_by_slug(new_slug)
        
        if self.current_window:
            self.position = Position()
            self.result = WindowResult(
                slug=self.current_window.slug,
                start_ts=self.current_window.start_ts,
                end_ts=self.current_window.end_ts,
                strategy=self.strategy,
            )
            self.bid_up = None
            self.bid_down = None
            self.first_leg_ts = 0.0
            
            self._log("WINDOW_START", {
                "slug": new_slug,
                "strategy": self.strategy,
                "params": {
                    "max_risk_usd": self.max_risk_usd_per_window,
                    "min_edge": self.min_edge,
                    "edge_target": self.edge_target,
                }
            })
            
            logger.info(f"Started window: {new_slug} (strategy={self.strategy})")
    
    def _finalize_result(self):
        """Finalize the result for current window."""
        self.result.final_position = self.position
        self.result.hedged = self.position.is_hedged
        self.result.achieved_pair_cost = self.position.pair_cost
        self.result.guaranteed_profit = self.position.guaranteed_profit
        self.result.max_loss = self.position.max_loss
    
    # =========================================================================
    # FIX #1: Hedge Feasibility Gate
    # =========================================================================
    
    def _check_hedge_feasibility(self, tick: OrderBookTick) -> Tuple[bool, str]:
        """
        Check if entering a position has a credible hedge path.
        
        Returns (can_enter, reason).
        """
        instant_pair_cost = tick.ask_up + tick.ask_down
        edge = 1.0 - instant_pair_cost
        
        if edge >= self.min_edge:
            # Instant arb possible (rare but check)
            return True, f"instant_arb_available (edge={edge:.4f})"
        
        # No instant arb - check if we're in maker mode or have rescue path
        if self.strategy == "M":
            # Maker mode: we're posting bids, not taking asks
            # Entry is feasible if our bid prices sum to <= 1 - edge_target
            return True, "maker_mode"
        
        # Variant L: check rescue feasibility
        # If we take cheap side, can we rescue-hedge within loss_cap?
        cheaper_side = Side.UP if tick.ask_up < tick.ask_down else Side.DOWN
        cheap_price = min(tick.ask_up, tick.ask_down)
        expensive_price = max(tick.ask_up, tick.ask_down)
        
        # If we buy cheap and have to rescue at expensive, what's the loss?
        rescue_pair_cost = cheap_price + expensive_price
        rescue_loss_per_share = max(0, rescue_pair_cost - 1.0)
        
        if rescue_loss_per_share * self.bid_clip_size <= self.loss_cap:
            return True, f"rescue_within_cap (rescue_loss=${rescue_loss_per_share * self.bid_clip_size:.2f})"
        
        return False, f"no_hedge_path (instant_pair={instant_pair_cost:.4f}, rescue_loss=${rescue_loss_per_share * self.bid_clip_size:.2f})"
    
    # =========================================================================
    # FIX #2: Per-Window Inventory Cap
    # =========================================================================
    
    def _check_risk_cap(self) -> Tuple[bool, str]:
        """
        Check if we've exceeded per-window risk limits.
        
        Returns (within_limits, reason).
        """
        if self.position.unhedged_exposure > self.max_risk_usd_per_window:
            return False, f"unhedged_exposure_exceeded (${self.position.unhedged_exposure:.2f} > ${self.max_risk_usd_per_window})"
        
        if self.position.unhedged_qty > self.max_unhedged_qty:
            return False, f"unhedged_qty_exceeded ({self.position.unhedged_qty} > {self.max_unhedged_qty})"
        
        return True, "within_limits"
    
    # =========================================================================
    # FIX #3: Variant M - Maker Full-Set
    # =========================================================================
    
    def _compute_maker_bids(self, tick: OrderBookTick) -> Tuple[float, float]:
        """
        Compute bid prices for maker strategy.
        
        Constraint: bid_up + bid_down <= 1 - edge_target
        """
        # Use mid prices as starting point
        mid_up = (tick.bid_up + tick.ask_up) / 2 if tick.bid_up > 0 else tick.ask_up - 0.01
        mid_down = (tick.bid_down + tick.ask_down) / 2 if tick.bid_down > 0 else tick.ask_down - 0.01
        
        # Initial bids slightly below mid
        delta = 0.01
        bid_up = mid_up - delta
        bid_down = mid_down - delta
        
        # Enforce constraint: bid_up + bid_down <= 1 - edge_target
        max_sum = 1.0 - self.edge_target
        current_sum = bid_up + bid_down
        
        if current_sum > max_sum:
            # Reduce proportionally
            excess = current_sum - max_sum
            bid_up -= excess / 2
            bid_down -= excess / 2
        
        # Clamp to reasonable range
        bid_up = max(0.01, min(0.99, bid_up))
        bid_down = max(0.01, min(0.99, bid_down))
        
        return bid_up, bid_down
    
    def _check_maker_fills(self, tick: OrderBookTick):
        """
        Check if our maker bids got filled (cross-through model).
        
        Fill a bid only if ask <= our bid (the market crossed through us).
        """
        filled_any = False
        
        # Check Up bid
        if self.bid_up and not self.bid_up.filled:
            if tick.ask_up <= self.bid_up.price:
                # Filled!
                self.bid_up.filled = True
                self.bid_up.filled_ts = tick.ts
                self.bid_up.fill_price = self.bid_up.price  # We get our bid price
                
                self.position.record_fill(Side.UP, self.bid_up.size, self.bid_up.price)
                self.result.bids_filled_up += 1
                filled_any = True
                
                self._log("MAKER_FILL", {
                    "side": "up",
                    "bid_price": self.bid_up.price,
                    "ask_at_fill": tick.ask_up,
                    "size": self.bid_up.size,
                    "position": self.position.to_dict(),
                })
                
                logger.info(f"MAKER FILL: Up @ {self.bid_up.price:.4f} (ask crossed to {tick.ask_up:.4f})")
        
        # Check Down bid
        if self.bid_down and not self.bid_down.filled:
            if tick.ask_down <= self.bid_down.price:
                # Filled!
                self.bid_down.filled = True
                self.bid_down.filled_ts = tick.ts
                self.bid_down.fill_price = self.bid_down.price
                
                self.position.record_fill(Side.DOWN, self.bid_down.size, self.bid_down.price)
                self.result.bids_filled_down += 1
                filled_any = True
                
                self._log("MAKER_FILL", {
                    "side": "down",
                    "bid_price": self.bid_down.price,
                    "ask_at_fill": tick.ask_down,
                    "size": self.bid_down.size,
                    "position": self.position.to_dict(),
                })
                
                logger.info(f"MAKER FILL: Down @ {self.bid_down.price:.4f} (ask crossed to {tick.ask_down:.4f})")
        
        # Check if we completed a pair
        if self.position.is_hedged:
            completed = int(self.position.min_qty / self.bid_clip_size)
            if completed > self.result.pairs_completed:
                new_pairs = completed - self.result.pairs_completed
                self.result.pairs_completed = completed
                
                self._log("PAIR_COMPLETED", {
                    "pairs": completed,
                    "pair_cost": self.position.pair_cost,
                    "guaranteed_profit": self.position.guaranteed_profit,
                })
                
                logger.info(f"PAIR COMPLETE: {completed} pairs @ cost={self.position.pair_cost:.4f}, GP=${self.position.guaranteed_profit:.2f}")
        
        return filled_any
    
    def _update_maker_bids(self, tick: OrderBookTick):
        """
        Post or update maker bids.
        
        Strategy:
        - If no bids posted, compute and post both
        - If one side filled, improve the other side bid to complete pair faster
        - Always maintain bid_up + bid_down <= 1 - edge_target
        """
        # Check risk cap
        within_cap, reason = self._check_risk_cap()
        if not within_cap:
            return  # Stop posting new bids
        
        bid_up_price, bid_down_price = self._compute_maker_bids(tick)
        
        # If one side already filled, improve the other to complete pair
        if self.bid_up and self.bid_up.filled and (not self.bid_down or not self.bid_down.filled):
            # Up filled, improve Down bid
            bid_down_price = min(
                bid_down_price + self.bid_improve_delta,
                1.0 - self.edge_target - self.bid_up.fill_price  # Maintain constraint
            )
        
        if self.bid_down and self.bid_down.filled and (not self.bid_up or not self.bid_up.filled):
            # Down filled, improve Up bid
            bid_up_price = min(
                bid_up_price + self.bid_improve_delta,
                1.0 - self.edge_target - self.bid_down.fill_price
            )
        
        # Post/update Up bid
        if not self.bid_up or self.bid_up.filled:
            self.bid_up = MakerBid(
                side=Side.UP,
                price=bid_up_price,
                size=self.bid_clip_size,
                posted_ts=tick.ts,
            )
            self.result.bids_posted += 1
        
        # Post/update Down bid
        if not self.bid_down or self.bid_down.filled:
            self.bid_down = MakerBid(
                side=Side.DOWN,
                price=bid_down_price,
                size=self.bid_clip_size,
                posted_ts=tick.ts,
            )
            self.result.bids_posted += 1
    
    def _process_variant_m(self, tick: OrderBookTick):
        """Process a tick for Variant M (maker full-set)."""
        # First check for fills
        self._check_maker_fills(tick)
        
        # Then update/post bids if needed
        if tick.seconds_remaining > self.stop_buffer_seconds:
            self._update_maker_bids(tick)
    
    # =========================================================================
    # Variant L - Legging with Rescue
    # =========================================================================
    
    def _process_variant_l(self, tick: OrderBookTick):
        """
        Process a tick for Variant L (legging with rescue bounds).
        
        Rules:
        1. Only enter if hedge is feasible (instant arb or rescue within loss_cap)
        2. If one leg taken, rescue within rescue_window_seconds
        3. Respect per-window risk cap
        """
        self.result.entry_attempts += 1
        
        # Check hedge feasibility (Fix #1)
        feasible, reason = self._check_hedge_feasibility(tick)
        if not feasible:
            self.result.entry_blocked_by_feasibility += 1
            return
        
        # Check risk cap (Fix #2)
        within_cap, cap_reason = self._check_risk_cap()
        if not within_cap:
            self.result.entry_blocked_by_risk_cap += 1
            return
        
        # Check if we need to rescue (time-based)
        if self.first_leg_ts > 0 and not self.position.is_hedged:
            elapsed = tick.ts - self.first_leg_ts
            if elapsed >= self.rescue_window_seconds:
                # Must rescue now
                self._execute_rescue(tick)
                return
        
        # Normal entry logic
        if not self.position.is_hedged:
            # Determine which side to take
            if self.position.q_up == 0 and self.position.q_down == 0:
                # No position yet - take the cheaper side
                if tick.ask_up <= tick.ask_down:
                    self._take_leg(Side.UP, tick.ask_up, tick)
                else:
                    self._take_leg(Side.DOWN, tick.ask_down, tick)
            
            elif self.position.q_up > 0 and self.position.q_down == 0:
                # Have Up, need Down
                rescue_cost = self.position.avg_up + tick.ask_down
                if rescue_cost <= 1.0 - self.min_edge:
                    # Can complete hedge profitably
                    self._take_leg(Side.DOWN, tick.ask_down, tick)
            
            elif self.position.q_down > 0 and self.position.q_up == 0:
                # Have Down, need Up
                rescue_cost = tick.ask_up + self.position.avg_down
                if rescue_cost <= 1.0 - self.min_edge:
                    # Can complete hedge profitably
                    self._take_leg(Side.UP, tick.ask_up, tick)
    
    def _take_leg(self, side: Side, price: float, tick: OrderBookTick):
        """Take one leg of the trade."""
        shares = self.bid_clip_size
        
        self.position.record_fill(side, shares, price)
        
        if self.first_leg_ts == 0:
            self.first_leg_ts = tick.ts
        
        self._log("LEG_TAKEN", {
            "side": side.value,
            "price": price,
            "shares": shares,
            "position": self.position.to_dict(),
        })
        
        logger.info(f"LEG: {side.value} @ {price:.4f}, pos={self.position.q_up}/{self.position.q_down}")
    
    def _execute_rescue(self, tick: OrderBookTick):
        """Force-hedge to limit losses."""
        self.result.rescue_triggered += 1
        
        if self.position.q_up > self.position.q_down:
            # Need more Down
            needed = self.position.q_up - self.position.q_down
            price = tick.ask_down
            self.position.record_fill(Side.DOWN, needed, price)
            side = "down"
        else:
            # Need more Up
            needed = self.position.q_down - self.position.q_up
            price = tick.ask_up
            self.position.record_fill(Side.UP, needed, price)
            side = "up"
        
        self._log("RESCUE", {
            "side": side,
            "shares": needed,
            "price": price,
            "pair_cost": self.position.pair_cost,
            "guaranteed_profit": self.position.guaranteed_profit,
        })
        
        logger.warning(f"RESCUE: Bought {needed} {side} @ {price:.4f}, GP=${self.position.guaranteed_profit:.2f}")
    
    # =========================================================================
    # Main Processing
    # =========================================================================
    
    def process_tick(self, tick: OrderBookTick):
        """Process a single tick."""
        if not self.result or not self.position:
            return
        
        self.result.ticks_seen += 1
        
        # Price sanity check (Fix #5)
        if tick.ask_up <= 0 or tick.ask_down <= 0:
            logger.warning(f"Invalid prices: ask_up={tick.ask_up}, ask_down={tick.ask_down}")
            return
        
        if tick.ask_up >= 1.0 or tick.ask_down >= 1.0:
            logger.warning(f"Price at boundary: ask_up={tick.ask_up}, ask_down={tick.ask_down}")
        
        # Stop trading near window end
        if tick.seconds_remaining <= self.stop_buffer_seconds:
            return
        
        # Dispatch to strategy
        if self.strategy == "M":
            self._process_variant_m(tick)
        else:
            self._process_variant_l(tick)
    
    def run_continuous(self, duration_minutes: int = 60, windows_target: int = 0):
        """Run paper trading continuously."""
        print("\n" + "="*70)
        print(f"Engine V2 - Strategy: {self.strategy}")
        print("="*70)
        print(f"Max risk per window: ${self.max_risk_usd_per_window}")
        print(f"Min edge: {self.min_edge*100:.1f}%")
        if self.strategy == "M":
            print(f"Edge target: {self.edge_target*100:.1f}%")
            print(f"Bid clip size: {self.bid_clip_size}")
        else:
            print(f"Rescue window: {self.rescue_window_seconds}s")
            print(f"Loss cap: ${self.loss_cap}")
        print(f"Output: {self.log_path}")
        print("="*70 + "\n")
        
        start_time = time.time()
        duration_sec = duration_minutes * 60
        poll_sec = 0.5
        
        try:
            while True:
                elapsed = time.time() - start_time
                
                if windows_target > 0 and len(self.results) >= windows_target:
                    break
                if elapsed >= duration_sec and windows_target == 0:
                    break
                
                current_slug = get_current_window_slug()
                
                if not self.current_window or self.current_window.slug != current_slug:
                    self._switch_window(current_slug)
                
                if not self.current_window:
                    time.sleep(poll_sec)
                    continue
                
                if self.current_window.is_finished():
                    time.sleep(poll_sec)
                    continue
                
                tick = self.fetcher.fetch_tick(self.current_window)
                if tick and tick.ask_up > 0:
                    self.process_tick(tick)
                    
                    if self.result and self.result.ticks_seen % 30 == 0:
                        self._print_status(tick)
                
                time.sleep(poll_sec)
        
        except KeyboardInterrupt:
            print("\n\nStopped by user.")
        
        finally:
            if self.result and self.position:
                self._finalize_result()
                self.results.append(self.result)
            
            if self.log_file:
                self.log_file.close()
            
            self._print_summary()
            self._save_results()
    
    def _print_status(self, tick: OrderBookTick):
        ts = datetime.now().strftime("%H:%M:%S")
        pos = self.position
        
        if self.strategy == "M":
            bid_info = ""
            if self.bid_up:
                bid_info += f"BidUp={self.bid_up.price:.2f}{'[F]' if self.bid_up.filled else ''} "
            if self.bid_down:
                bid_info += f"BidDn={self.bid_down.price:.2f}{'[F]' if self.bid_down.filled else ''}"
            
            print(f"[{ts}] W{len(self.results)+1} | "
                  f"{tick.seconds_remaining:.0f}s | "
                  f"Ask: {tick.ask_up:.2f}/{tick.ask_down:.2f} | "
                  f"{bid_info} | "
                  f"Pairs: {self.result.pairs_completed}")
        else:
            print(f"[{ts}] W{len(self.results)+1} | "
                  f"{tick.seconds_remaining:.0f}s | "
                  f"Ask: {tick.ask_up:.2f}/{tick.ask_down:.2f} | "
                  f"Pos: {pos.q_up:.0f}/{pos.q_down:.0f} | "
                  f"GP: ${pos.guaranteed_profit:.2f}")
    
    def _print_summary(self):
        """Print comprehensive summary."""
        print("\n" + "="*70)
        print("PAPER TRADING SUMMARY (V2)")
        print("="*70)
        
        print(f"\nStrategy: {self.strategy}")
        print(f"Windows completed: {len(self.results)}")
        print(f"Log file: {self.log_path}")
        
        if not self.results:
            print("\nNo complete windows.")
            return
        
        # Aggregate metrics
        total_hedged = sum(1 for r in self.results if r.hedged)
        total_gp = sum(r.guaranteed_profit for r in self.results if r.hedged)
        total_loss = sum(r.max_loss for r in self.results if not r.hedged)
        
        print(f"\n--- Hedge Success ---")
        print(f"  Windows hedged: {total_hedged} / {len(self.results)} ({total_hedged/len(self.results)*100:.1f}%)")
        print(f"  Total GP (hedged): ${total_gp:.2f}")
        print(f"  Total max loss (unhedged): ${total_loss:.2f}")
        
        if self.strategy == "M":
            total_fills_up = sum(r.bids_filled_up for r in self.results)
            total_fills_down = sum(r.bids_filled_down for r in self.results)
            total_pairs = sum(r.pairs_completed for r in self.results)
            
            print(f"\n--- Maker Stats ---")
            print(f"  Total Up fills: {total_fills_up}")
            print(f"  Total Down fills: {total_fills_down}")
            print(f"  Total pairs completed: {total_pairs}")
        
        else:
            total_blocked_feasibility = sum(r.entry_blocked_by_feasibility for r in self.results)
            total_blocked_risk = sum(r.entry_blocked_by_risk_cap for r in self.results)
            total_rescues = sum(r.rescue_triggered for r in self.results)
            
            print(f"\n--- Legging Stats ---")
            print(f"  Blocked by feasibility: {total_blocked_feasibility}")
            print(f"  Blocked by risk cap: {total_blocked_risk}")
            print(f"  Rescues triggered: {total_rescues}")
        
        # Per-window breakdown
        print(f"\n--- Per-Window Results ---")
        for r in self.results[-10:]:
            status = "HEDGED" if r.hedged else "UNHEDGED"
            if r.hedged:
                print(f"  {r.slug}: {status}, cost={r.achieved_pair_cost:.4f}, GP=${r.guaranteed_profit:.2f}")
            else:
                print(f"  {r.slug}: {status}, max_loss=${r.max_loss:.2f}")
    
    def _save_results(self):
        """Save results to JSON."""
        path = self.output_dir / f"results_v2_{datetime.now(timezone.utc).strftime('%Y%m%d_%H%M%S')}.json"
        
        data = {
            "strategy": self.strategy,
            "params": {
                "max_risk_usd_per_window": self.max_risk_usd_per_window,
                "max_unhedged_qty": self.max_unhedged_qty,
                "min_edge": self.min_edge,
                "edge_target": self.edge_target,
                "loss_cap": self.loss_cap,
            },
            "summary": {
                "windows": len(self.results),
                "hedged": sum(1 for r in self.results if r.hedged),
                "total_gp": sum(r.guaranteed_profit for r in self.results if r.hedged),
                "total_max_loss": sum(r.max_loss for r in self.results if not r.hedged),
            },
            "results": [r.to_dict() for r in self.results],
        }
        
        with open(path, "w") as f:
            json.dump(data, f, indent=2)
        
        print(f"\nResults saved: {path}")


def run_maker(duration: int = 60, windows: int = 0, edge: float = 0.01):
    """Run Variant M (maker full-set)."""
    engine = EngineV2(strategy="M", edge_target=edge)
    engine.run_continuous(duration_minutes=duration, windows_target=windows)


def run_legging(duration: int = 60, windows: int = 0):
    """Run Variant L (legging with rescue)."""
    engine = EngineV2(strategy="L")
    engine.run_continuous(duration_minutes=duration, windows_target=windows)


if __name__ == "__main__":
    import argparse
    
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s | %(levelname)s | %(message)s",
        datefmt="%H:%M:%S"
    )
    
    parser = argparse.ArgumentParser(description="Engine V2 - Properly constrained paper trading")
    parser.add_argument("--strategy", choices=["M", "L"], default="M", 
                       help="M=Maker full-set, L=Legging with rescue")
    parser.add_argument("--duration", type=int, default=60, help="Duration in minutes")
    parser.add_argument("--windows", type=int, default=0, help="Stop after N windows (0=use duration)")
    parser.add_argument("--edge-target", type=float, default=0.01, help="Target edge for maker")
    parser.add_argument("--max-risk", type=float, default=2.0, help="Max USD risk per window")
    
    args = parser.parse_args()
    
    engine = EngineV2(
        strategy=args.strategy,
        edge_target=args.edge_target,
        max_risk_usd_per_window=args.max_risk,
    )
    engine.run_continuous(duration_minutes=args.duration, windows_target=args.windows)

