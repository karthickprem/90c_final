"""
Engine V3 - Adaptive Maker Pair-Capture with Proper Metrics

Key improvements from V2:
1. Adaptive completion mechanic - raise other-side bid after first fill
2. Time-based aggressiveness ladder - tighten as seconds_remaining decreases
3. Two fill models: L (cross-through) and M (persistent N ticks)
4. Proper metrics: P(complete|first fill), time-to-complete, locked edge
5. Volatility proxy + price_to_beat logging
6. Conditional kill/success criteria

This is the real maker pair-capture strategy.
"""

import logging
import time
import json
from pathlib import Path
from datetime import datetime, timezone
from typing import Optional, List, Dict, Tuple
from dataclasses import dataclass, field
from enum import Enum
from collections import deque

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


class FillModel(Enum):
    MODEL_L = "L"  # Cross-through only (conservative)
    MODEL_M = "M"  # Persistent cross-through (medium realism)


@dataclass
class BidState:
    """State of a maker bid."""
    side: Side
    price: float
    size: float
    posted_ts: float
    
    # For Model M: track consecutive cross-through ticks
    cross_ticks: int = 0
    
    # Fill state
    filled: bool = False
    filled_ts: float = 0.0
    fill_price: float = 0.0
    fill_model: Optional[FillModel] = None


@dataclass
class Position:
    """Position with proper tracking."""
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
    def hedged_qty(self) -> float:
        return min(self.q_up, self.q_down)
    
    @property
    def unhedged_qty(self) -> float:
        return abs(self.q_up - self.q_down)
    
    @property
    def locked_edge(self) -> float:
        """Edge locked in per hedged pair."""
        if self.hedged_qty > 0:
            return 1.0 - (self.avg_up + self.avg_down)
        return 0
    
    @property
    def locked_profit(self) -> float:
        """Total locked profit from hedged pairs."""
        return self.hedged_qty * self.locked_edge
    
    @property
    def max_loss(self) -> float:
        """Max loss from unhedged exposure."""
        if self.q_up > self.q_down:
            return (self.q_up - self.q_down) * self.avg_up
        else:
            return (self.q_down - self.q_up) * self.avg_down
    
    def record_fill(self, side: Side, shares: float, price: float):
        if side == Side.UP:
            self.q_up += shares
            self.cost_up += shares * price
        else:
            self.q_down += shares
            self.cost_down += shares * price


@dataclass
class WindowMetrics:
    """Comprehensive metrics for a single window."""
    slug: str
    start_ts: int
    end_ts: int
    
    # Market context
    volatility_proxy: float = 0.0  # Max mid swing during window
    price_to_beat: float = 0.0
    current_price_at_start: float = 0.0
    price_distance: float = 0.0  # |current - price_to_beat|
    
    # Tick tracking
    ticks_seen: int = 0
    mid_prices: List[float] = field(default_factory=list)
    
    # Fill events (both models)
    first_leg_fills_L: int = 0
    first_leg_fills_M: int = 0
    completed_pairs_L: int = 0
    completed_pairs_M: int = 0
    
    # Timing
    first_fill_ts: float = 0.0
    completion_ts: float = 0.0
    time_to_complete: float = 0.0  # seconds from first fill to completion
    time_unhedged: float = 0.0  # total seconds spent unhedged
    
    # Position
    final_locked_edge: float = 0.0
    final_locked_profit: float = 0.0
    max_unhedged_exposure: float = 0.0
    
    # Bid tracking
    bid_updates: int = 0
    
    def compute_volatility(self):
        """Compute volatility proxy from mid prices."""
        if len(self.mid_prices) < 2:
            self.volatility_proxy = 0
            return
        
        min_mid = min(self.mid_prices)
        max_mid = max(self.mid_prices)
        self.volatility_proxy = max_mid - min_mid
    
    def to_dict(self) -> dict:
        return {
            "slug": self.slug,
            "volatility_proxy": self.volatility_proxy,
            "price_distance": self.price_distance,
            "ticks_seen": self.ticks_seen,
            "first_leg_fills_L": self.first_leg_fills_L,
            "first_leg_fills_M": self.first_leg_fills_M,
            "completed_pairs_L": self.completed_pairs_L,
            "completed_pairs_M": self.completed_pairs_M,
            "time_to_complete": self.time_to_complete,
            "time_unhedged": self.time_unhedged,
            "final_locked_edge": self.final_locked_edge,
            "final_locked_profit": self.final_locked_profit,
            "max_unhedged_exposure": self.max_unhedged_exposure,
            "bid_updates": self.bid_updates,
        }


class EngineV3:
    """
    Adaptive Maker Pair-Capture Engine.
    
    Key features:
    - Posts two-sided bids where bid_up + bid_down <= 1 - edge_floor
    - After first leg fills, adaptively raises the other-side bid
    - Time-based aggressiveness: tighten as window nears end
    - Two fill models: L (cross-through) and M (persistent)
    - Proper conditional metrics
    """
    
    def __init__(
        self,
        # Edge constraints
        edge_floor: float = 0.005,  # Minimum locked edge (0.5%)
        initial_edge_target: float = 0.015,  # Start with higher edge
        
        # Bid sizing
        clip_size: float = 5.0,
        
        # Risk limits
        max_risk_per_window: float = 3.0,
        max_unhedged_qty: float = 20.0,
        
        # Completion aggressiveness
        completion_ladder_steps: int = 5,  # Number of bid improvements
        completion_tighten_rate: float = 0.002,  # Improve bid by this each step
        
        # Time controls
        stop_new_entries_seconds: float = 120,  # Stop new entries 2 min before end
        force_cancel_seconds: float = 30,  # Cancel all bids 30s before end
        
        # Fill model parameters
        model_m_persist_ticks: int = 3,  # Require N consecutive cross-through ticks
        
        # Output
        output_dir: str = "pm_results_v3",
    ):
        self.edge_floor = edge_floor
        self.initial_edge_target = initial_edge_target
        self.clip_size = clip_size
        self.max_risk_per_window = max_risk_per_window
        self.max_unhedged_qty = max_unhedged_qty
        self.completion_ladder_steps = completion_ladder_steps
        self.completion_tighten_rate = completion_tighten_rate
        self.stop_new_entries_seconds = stop_new_entries_seconds
        self.force_cancel_seconds = force_cancel_seconds
        self.model_m_persist_ticks = model_m_persist_ticks
        
        self.output_dir = Path(output_dir)
        self.output_dir.mkdir(exist_ok=True)
        
        self.fetcher = MarketFetcher()
        
        # State
        self.current_window: Optional[Window15Min] = None
        self.position_L: Optional[Position] = None  # Model L position
        self.position_M: Optional[Position] = None  # Model M position
        self.metrics: Optional[WindowMetrics] = None
        
        # Bid state
        self.bid_up: Optional[BidState] = None
        self.bid_down: Optional[BidState] = None
        
        # Completion state
        self.first_leg_side: Optional[Side] = None
        self.first_leg_price: float = 0.0
        self.first_leg_ts: float = 0.0
        self.completion_step: int = 0
        self.last_unhedged_check_ts: float = 0.0
        
        # All results
        self.all_metrics: List[WindowMetrics] = []
        
        # Logging
        self.log_file = None
        self._open_log()
    
    def _open_log(self):
        ts = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
        self.log_path = self.output_dir / f"trades_v3_{ts}.jsonl"
        self.log_file = open(self.log_path, "a")
    
    def _log(self, event: str, data: dict):
        entry = {
            "ts": time.time(),
            "event": event,
            "window": self.current_window.slug if self.current_window else None,
            **data
        }
        if self.log_file:
            self.log_file.write(json.dumps(entry) + "\n")
            self.log_file.flush()
    
    # =========================================================================
    # Window Management
    # =========================================================================
    
    def _start_window(self, slug: str):
        """Initialize a new window."""
        # Finalize previous
        if self.metrics:
            self._finalize_window()
            self.all_metrics.append(self.metrics)
        
        # Fetch window
        self.current_window = self.fetcher.fetch_market_by_slug(slug)
        if not self.current_window:
            return
        
        # Reset state
        self.position_L = Position()
        self.position_M = Position()
        self.metrics = WindowMetrics(
            slug=slug,
            start_ts=self.current_window.start_ts,
            end_ts=self.current_window.end_ts,
        )
        
        self.bid_up = None
        self.bid_down = None
        self.first_leg_side = None
        self.first_leg_price = 0.0
        self.first_leg_ts = 0.0
        self.completion_step = 0
        self.last_unhedged_check_ts = time.time()
        
        self._log("WINDOW_START", {"slug": slug})
        logger.info(f"Started window: {slug}")
    
    def _finalize_window(self):
        """Finalize metrics for the window."""
        self.metrics.compute_volatility()
        
        # Model L metrics
        if self.position_L.hedged_qty > 0:
            self.metrics.final_locked_edge = self.position_L.locked_edge
            self.metrics.final_locked_profit = self.position_L.locked_profit
        
        self._log("WINDOW_END", self.metrics.to_dict())
    
    # =========================================================================
    # Bid Computation with Adaptive Completion
    # =========================================================================
    
    def _compute_initial_bids(self, tick: OrderBookTick) -> Tuple[float, float]:
        """
        Compute initial bid prices.
        
        Constraint: bid_up + bid_down <= 1 - initial_edge_target
        """
        mid_up = (tick.bid_up + tick.ask_up) / 2 if tick.bid_up > 0 else tick.ask_up - 0.01
        mid_down = (tick.bid_down + tick.ask_down) / 2 if tick.bid_down > 0 else tick.ask_down - 0.01
        
        # Start with bids slightly below mid
        delta = 0.01
        bid_up = mid_up - delta
        bid_down = mid_down - delta
        
        # Enforce constraint
        max_sum = 1.0 - self.initial_edge_target
        current_sum = bid_up + bid_down
        
        if current_sum > max_sum:
            excess = current_sum - max_sum
            bid_up -= excess / 2
            bid_down -= excess / 2
        
        return max(0.01, bid_up), max(0.01, bid_down)
    
    def _compute_completion_bid(self, tick: OrderBookTick, time_remaining: float) -> float:
        """
        Compute the completion bid for the unfilled side.
        
        After first leg fills at first_leg_price, we must complete at:
        completion_bid <= 1 - edge_floor - first_leg_price
        
        We ladder up the bid as time runs out.
        """
        if not self.first_leg_side:
            return 0
        
        # Max we can pay for completion while maintaining edge_floor
        max_completion_price = 1.0 - self.edge_floor - self.first_leg_price
        
        # Time-based aggressiveness
        # More aggressive as time runs out
        window_duration = 900  # 15 minutes
        time_fraction = max(0, min(1, time_remaining / window_duration))
        
        # Early: bid at mid - delta
        # Late: bid at max_completion_price
        if self.first_leg_side == Side.UP:
            mid = (tick.bid_down + tick.ask_down) / 2 if tick.bid_down > 0 else tick.ask_down - 0.01
        else:
            mid = (tick.bid_up + tick.ask_up) / 2 if tick.bid_up > 0 else tick.ask_up - 0.01
        
        # Interpolate between conservative and aggressive
        conservative_bid = mid - 0.02
        aggressive_bid = max_completion_price
        
        # As time_fraction decreases (less time left), we get more aggressive
        aggressiveness = 1.0 - time_fraction  # 0 at start, 1 at end
        
        # Also factor in completion_step
        step_bonus = self.completion_step * self.completion_tighten_rate
        
        completion_bid = conservative_bid + (aggressive_bid - conservative_bid) * aggressiveness + step_bonus
        
        # Never exceed max
        return min(max_completion_price, max(0.01, completion_bid))
    
    # =========================================================================
    # Fill Detection with Two Models
    # =========================================================================
    
    def _check_fill_model_L(self, bid: BidState, current_ask: float) -> bool:
        """
        Model L (conservative): Cross-through only.
        
        Fill if ask <= bid price.
        """
        return current_ask <= bid.price
    
    def _check_fill_model_M(self, bid: BidState, current_ask: float) -> bool:
        """
        Model M (medium realism): Persistent cross-through.
        
        Fill only if ask <= bid for N consecutive ticks.
        """
        if current_ask <= bid.price:
            bid.cross_ticks += 1
        else:
            bid.cross_ticks = 0
        
        return bid.cross_ticks >= self.model_m_persist_ticks
    
    def _process_fills(self, tick: OrderBookTick):
        """Process fills for both models."""
        ts = tick.ts
        
        # Check Up bid
        if self.bid_up and not self.bid_up.filled:
            # Model L
            if self._check_fill_model_L(self.bid_up, tick.ask_up):
                self._record_fill(Side.UP, self.bid_up.price, ts, FillModel.MODEL_L)
            
            # Model M
            if self._check_fill_model_M(self.bid_up, tick.ask_up):
                self._record_fill(Side.UP, self.bid_up.price, ts, FillModel.MODEL_M)
                self.bid_up.filled = True
                self.bid_up.filled_ts = ts
                self.bid_up.fill_price = self.bid_up.price
                self.bid_up.fill_model = FillModel.MODEL_M
        
        # Check Down bid
        if self.bid_down and not self.bid_down.filled:
            # Model L
            if self._check_fill_model_L(self.bid_down, tick.ask_down):
                self._record_fill(Side.DOWN, self.bid_down.price, ts, FillModel.MODEL_L)
            
            # Model M
            if self._check_fill_model_M(self.bid_down, tick.ask_down):
                self._record_fill(Side.DOWN, self.bid_down.price, ts, FillModel.MODEL_M)
                self.bid_down.filled = True
                self.bid_down.filled_ts = ts
                self.bid_down.fill_price = self.bid_down.price
                self.bid_down.fill_model = FillModel.MODEL_M
    
    def _record_fill(self, side: Side, price: float, ts: float, model: FillModel):
        """Record a fill in the appropriate position."""
        pos = self.position_L if model == FillModel.MODEL_L else self.position_M
        
        # Check if this is first leg
        was_empty = pos.q_up == 0 and pos.q_down == 0
        
        pos.record_fill(side, self.clip_size, price)
        
        if was_empty:
            # First fill
            if model == FillModel.MODEL_L:
                self.metrics.first_leg_fills_L += 1
            else:
                self.metrics.first_leg_fills_M += 1
            
            if self.first_leg_ts == 0:
                self.first_leg_ts = ts
                self.first_leg_side = side
                self.first_leg_price = price
                self.metrics.first_fill_ts = ts
            
            self._log("FIRST_LEG_FILL", {
                "model": model.value,
                "side": side.value,
                "price": price,
            })
            
            logger.info(f"FIRST LEG ({model.value}): {side.value} @ {price:.4f}")
        
        # Check for pair completion
        if pos.hedged_qty > 0:
            if model == FillModel.MODEL_L:
                self.metrics.completed_pairs_L += 1
            else:
                self.metrics.completed_pairs_M += 1
            
            self.metrics.completion_ts = ts
            self.metrics.time_to_complete = ts - self.metrics.first_fill_ts
            
            self._log("PAIR_COMPLETE", {
                "model": model.value,
                "locked_edge": pos.locked_edge,
                "locked_profit": pos.locked_profit,
                "time_to_complete": self.metrics.time_to_complete,
            })
            
            logger.info(f"PAIR COMPLETE ({model.value}): edge={pos.locked_edge:.4f}, "
                       f"time={self.metrics.time_to_complete:.1f}s")
    
    # =========================================================================
    # Adaptive Bid Management
    # =========================================================================
    
    def _update_bids(self, tick: OrderBookTick):
        """Update bids based on current state."""
        time_remaining = tick.seconds_remaining
        
        # Don't update near end
        if time_remaining <= self.force_cancel_seconds:
            self.bid_up = None
            self.bid_down = None
            return
        
        # Track unhedged time
        now = tick.ts
        if self.first_leg_ts > 0 and self.position_M.hedged_qty == 0:
            self.metrics.time_unhedged += now - self.last_unhedged_check_ts
            self.metrics.max_unhedged_exposure = max(
                self.metrics.max_unhedged_exposure,
                self.position_M.max_loss
            )
        self.last_unhedged_check_ts = now
        
        # If we have a first leg fill, focus on completing
        if self.first_leg_side and self.position_M.hedged_qty == 0:
            self._update_completion_bid(tick, time_remaining)
        elif not self.first_leg_side:
            # No fill yet - post initial bids if allowed
            if time_remaining > self.stop_new_entries_seconds:
                self._post_initial_bids(tick)
    
    def _post_initial_bids(self, tick: OrderBookTick):
        """Post initial two-sided bids."""
        bid_up_price, bid_down_price = self._compute_initial_bids(tick)
        
        if not self.bid_up:
            self.bid_up = BidState(
                side=Side.UP,
                price=bid_up_price,
                size=self.clip_size,
                posted_ts=tick.ts,
            )
        
        if not self.bid_down:
            self.bid_down = BidState(
                side=Side.DOWN,
                price=bid_down_price,
                size=self.clip_size,
                posted_ts=tick.ts,
            )
    
    def _update_completion_bid(self, tick: OrderBookTick, time_remaining: float):
        """Update the completion bid for the unfilled side."""
        completion_price = self._compute_completion_bid(tick, time_remaining)
        
        if self.first_leg_side == Side.UP:
            # Need to complete Down
            if self.bid_down is None or self.bid_down.price < completion_price:
                self.bid_down = BidState(
                    side=Side.DOWN,
                    price=completion_price,
                    size=self.clip_size,
                    posted_ts=tick.ts,
                )
                self.completion_step += 1
                self.metrics.bid_updates += 1
                
                self._log("BID_UPDATE", {
                    "side": "down",
                    "new_price": completion_price,
                    "step": self.completion_step,
                    "time_remaining": time_remaining,
                })
        else:
            # Need to complete Up
            if self.bid_up is None or self.bid_up.price < completion_price:
                self.bid_up = BidState(
                    side=Side.UP,
                    price=completion_price,
                    size=self.clip_size,
                    posted_ts=tick.ts,
                )
                self.completion_step += 1
                self.metrics.bid_updates += 1
                
                self._log("BID_UPDATE", {
                    "side": "up",
                    "new_price": completion_price,
                    "step": self.completion_step,
                    "time_remaining": time_remaining,
                })
    
    # =========================================================================
    # Main Processing
    # =========================================================================
    
    def process_tick(self, tick: OrderBookTick):
        """Process a single tick."""
        if not self.metrics:
            return
        
        self.metrics.ticks_seen += 1
        
        # Track mid price for volatility
        mid = (tick.ask_up + tick.ask_down) / 2
        self.metrics.mid_prices.append(mid)
        
        # Check for fills
        self._process_fills(tick)
        
        # Update bids
        self._update_bids(tick)
    
    def run_continuous(self, duration_minutes: int = 60, windows_target: int = 0):
        """Run the engine continuously."""
        print("\n" + "="*70)
        print("Engine V3 - Adaptive Maker Pair-Capture")
        print("="*70)
        print(f"Edge floor: {self.edge_floor*100:.1f}%")
        print(f"Initial edge target: {self.initial_edge_target*100:.1f}%")
        print(f"Model M persist ticks: {self.model_m_persist_ticks}")
        print(f"Clip size: {self.clip_size}")
        print(f"Output: {self.log_path}")
        print("="*70 + "\n")
        
        start_time = time.time()
        duration_sec = duration_minutes * 60
        poll_sec = 0.5
        
        try:
            while True:
                elapsed = time.time() - start_time
                
                if windows_target > 0 and len(self.all_metrics) >= windows_target:
                    break
                if elapsed >= duration_sec and windows_target == 0:
                    break
                
                current_slug = get_current_window_slug()
                
                if not self.current_window or self.current_window.slug != current_slug:
                    self._start_window(current_slug)
                
                if not self.current_window:
                    time.sleep(poll_sec)
                    continue
                
                if self.current_window.is_finished():
                    time.sleep(poll_sec)
                    continue
                
                tick = self.fetcher.fetch_tick(self.current_window)
                if tick and tick.ask_up > 0:
                    self.process_tick(tick)
                    
                    if self.metrics.ticks_seen % 30 == 0:
                        self._print_status(tick)
                
                time.sleep(poll_sec)
        
        except KeyboardInterrupt:
            print("\n\nStopped by user.")
        
        finally:
            if self.metrics:
                self._finalize_window()
                self.all_metrics.append(self.metrics)
            
            if self.log_file:
                self.log_file.close()
            
            self._print_summary()
            self._save_results()
    
    def _print_status(self, tick: OrderBookTick):
        ts = datetime.now().strftime("%H:%M:%S")
        
        bid_info = ""
        if self.bid_up:
            bid_info += f"U:{self.bid_up.price:.2f}"
            if self.bid_up.filled:
                bid_info += "[F]"
        if self.bid_down:
            bid_info += f" D:{self.bid_down.price:.2f}"
            if self.bid_down.filled:
                bid_info += "[F]"
        
        print(f"[{ts}] W{len(self.all_metrics)+1} | "
              f"{tick.seconds_remaining:.0f}s | "
              f"Ask:{tick.ask_up:.2f}/{tick.ask_down:.2f} | "
              f"Bid:{bid_info} | "
              f"Pairs L:{self.metrics.completed_pairs_L} M:{self.metrics.completed_pairs_M}")
    
    def _print_summary(self):
        """Print comprehensive summary with conditional metrics."""
        print("\n" + "="*70)
        print("ENGINE V3 SUMMARY - CONDITIONAL METRICS")
        print("="*70)
        
        print(f"\nWindows: {len(self.all_metrics)}")
        print(f"Log: {self.log_path}")
        
        if not self.all_metrics:
            print("\nNo complete windows.")
            return
        
        # Aggregate by model
        for model_name, fill_key, complete_key in [
            ("Model L (conservative)", "first_leg_fills_L", "completed_pairs_L"),
            ("Model M (medium)", "first_leg_fills_M", "completed_pairs_M"),
        ]:
            total_first_fills = sum(getattr(m, fill_key) for m in self.all_metrics)
            total_completions = sum(getattr(m, complete_key) for m in self.all_metrics)
            
            print(f"\n--- {model_name} ---")
            print(f"  First-leg fills: {total_first_fills}")
            print(f"  Completed pairs: {total_completions}")
            
            if total_first_fills > 0:
                completion_rate = total_completions / total_first_fills * 100
                print(f"  P(complete | first fill): {completion_rate:.1f}%")
                
                # Time to complete
                completion_times = [m.time_to_complete for m in self.all_metrics 
                                   if getattr(m, complete_key) > 0 and m.time_to_complete > 0]
                if completion_times:
                    avg_time = sum(completion_times) / len(completion_times)
                    print(f"  Avg time to complete: {avg_time:.1f}s")
                
                # Locked edge
                locked_edges = [m.final_locked_edge for m in self.all_metrics 
                               if getattr(m, complete_key) > 0 and m.final_locked_edge > 0]
                if locked_edges:
                    avg_edge = sum(locked_edges) / len(locked_edges)
                    median_edge = sorted(locked_edges)[len(locked_edges)//2]
                    print(f"  Avg locked edge: {avg_edge*100:.2f} cents")
                    print(f"  Median locked edge: {median_edge*100:.2f} cents")
            else:
                print(f"  P(complete | first fill): N/A (no first fills)")
        
        # Time unhedged
        unhedged_times = [m.time_unhedged for m in self.all_metrics if m.time_unhedged > 0]
        if unhedged_times:
            print(f"\n--- Risk Metrics ---")
            print(f"  Avg time unhedged: {sum(unhedged_times)/len(unhedged_times):.1f}s")
            print(f"  Max time unhedged: {max(unhedged_times):.1f}s")
        
        max_exposures = [m.max_unhedged_exposure for m in self.all_metrics if m.max_unhedged_exposure > 0]
        if max_exposures:
            print(f"  Max unhedged exposure: ${max(max_exposures):.2f}")
        
        # Volatility analysis
        vol_data = [(m.volatility_proxy, m.completed_pairs_M) for m in self.all_metrics]
        if vol_data:
            high_vol = [v for v, c in vol_data if v > 0.02]
            low_vol = [v for v, c in vol_data if v <= 0.02]
            completions_high_vol = sum(c for v, c in vol_data if v > 0.02)
            completions_low_vol = sum(c for v, c in vol_data if v <= 0.02)
            
            print(f"\n--- Volatility Analysis ---")
            print(f"  High-vol windows (>2% swing): {len(high_vol)}, completions: {completions_high_vol}")
            print(f"  Low-vol windows (<=2% swing): {len(low_vol)}, completions: {completions_low_vol}")
        
        # Kill/Success criteria evaluation
        print(f"\n--- Kill/Success Criteria ---")
        total_first_fills_M = sum(m.first_leg_fills_M for m in self.all_metrics)
        total_completions_M = sum(m.completed_pairs_M for m in self.all_metrics)
        
        if total_first_fills_M >= 100:
            p_complete = total_completions_M / total_first_fills_M * 100
            if p_complete < 20:
                print(f"  KILL: P(complete|first fill) = {p_complete:.1f}% < 20%")
            elif p_complete > 50:
                locked_edges = [m.final_locked_edge for m in self.all_metrics if m.completed_pairs_M > 0]
                if locked_edges:
                    median_edge = sorted(locked_edges)[len(locked_edges)//2] * 100
                    if median_edge > 0.5:
                        print(f"  PROMISING: P(complete|first fill) = {p_complete:.1f}% > 50%, median edge = {median_edge:.1f} cents")
                    else:
                        print(f"  MARGINAL: P(complete|first fill) > 50% but edge only {median_edge:.1f} cents")
            else:
                print(f"  INCONCLUSIVE: P(complete|first fill) = {p_complete:.1f}%")
        else:
            print(f"  NEED MORE DATA: Only {total_first_fills_M} first-leg fills (need 100+)")
    
    def _save_results(self):
        """Save results to JSON."""
        path = self.output_dir / f"results_v3_{datetime.now(timezone.utc).strftime('%Y%m%d_%H%M%S')}.json"
        
        # Calculate summary stats
        total_first_L = sum(m.first_leg_fills_L for m in self.all_metrics)
        total_first_M = sum(m.first_leg_fills_M for m in self.all_metrics)
        total_complete_L = sum(m.completed_pairs_L for m in self.all_metrics)
        total_complete_M = sum(m.completed_pairs_M for m in self.all_metrics)
        
        data = {
            "params": {
                "edge_floor": self.edge_floor,
                "initial_edge_target": self.initial_edge_target,
                "model_m_persist_ticks": self.model_m_persist_ticks,
                "clip_size": self.clip_size,
            },
            "summary": {
                "windows": len(self.all_metrics),
                "model_L": {
                    "first_leg_fills": total_first_L,
                    "completed_pairs": total_complete_L,
                    "p_complete_given_first": total_complete_L / total_first_L if total_first_L > 0 else 0,
                },
                "model_M": {
                    "first_leg_fills": total_first_M,
                    "completed_pairs": total_complete_M,
                    "p_complete_given_first": total_complete_M / total_first_M if total_first_M > 0 else 0,
                },
            },
            "windows": [m.to_dict() for m in self.all_metrics],
        }
        
        with open(path, "w") as f:
            json.dump(data, f, indent=2)
        
        print(f"\nResults saved: {path}")


if __name__ == "__main__":
    import argparse
    
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s | %(levelname)s | %(message)s",
        datefmt="%H:%M:%S"
    )
    
    parser = argparse.ArgumentParser(description="Engine V3 - Adaptive Maker Pair-Capture")
    parser.add_argument("--duration", type=int, default=60, help="Duration in minutes")
    parser.add_argument("--windows", type=int, default=0, help="Stop after N windows")
    parser.add_argument("--edge-floor", type=float, default=0.005, help="Min edge (0.5%)")
    parser.add_argument("--persist-ticks", type=int, default=3, help="Model M persist ticks")
    
    args = parser.parse_args()
    
    engine = EngineV3(
        edge_floor=args.edge_floor,
        model_m_persist_ticks=args.persist_ticks,
    )
    engine.run_continuous(duration_minutes=args.duration, windows_target=args.windows)

