"""
Engine V4 - Production-Ready Maker Pair-Capture

Key improvements over V3:
1. Hard completion invariant: completion_bid <= 1 - edge_floor - P_fill (never violated)
2. Model Q (queue penalty): cross-through + size depletion + partial fill assumption
3. Quote cadence: min lifetime, max cancels per window
4. Metrics: edge_net distribution (median + p10), stratified by volatility + price distance
5. Event-driven run: target first-leg fills, not hours
6. Near-50/50 pre-filter: only trade windows with tight price_to_beat distance

Three fill models:
- L (optimistic): instant cross-through
- M (mid): persistent cross-through (N ticks)
- Q (pessimistic): cross-through + size depletion + partial fill
"""

import logging
import time
import json
import statistics
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
    L = "L"  # Optimistic: instant cross-through
    M = "M"  # Mid: persistent cross-through
    Q = "Q"  # Pessimistic: cross-through + queue penalty


class WindowMode(Enum):
    NORMAL = "normal"
    RESCUE = "rescue"
    STOPPED = "stopped"


@dataclass
class QuoteState:
    """State of a resting quote with lifecycle tracking."""
    side: Side
    price: float
    size: float
    posted_ts: float
    
    # For fill detection
    cross_ticks: int = 0
    initial_book_size: float = 0.0  # Book size when posted
    
    # Lifecycle
    cancelled: bool = False
    filled_L: bool = False
    filled_M: bool = False
    filled_Q: bool = False
    fill_ts: float = 0.0
    fill_price: float = 0.0
    partial_fill_qty: float = 0.0  # For Model Q partial fills


@dataclass
class Position:
    """Position tracking per model."""
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
    def edge_net(self) -> float:
        """Net edge per hedged pair."""
        if self.hedged_qty > 0:
            return 1.0 - (self.avg_up + self.avg_down)
        return 0
    
    @property
    def unhedged_qty(self) -> float:
        return abs(self.q_up - self.q_down)
    
    @property
    def max_loss(self) -> float:
        if self.q_up > self.q_down:
            return (self.q_up - self.q_down) * self.avg_up
        return (self.q_down - self.q_up) * self.avg_down
    
    def record_fill(self, side: Side, qty: float, price: float):
        if side == Side.UP:
            self.q_up += qty
            self.cost_up += qty * price
        else:
            self.q_down += qty
            self.cost_down += qty * price


@dataclass
class WindowMetrics:
    """Comprehensive metrics per window."""
    slug: str
    start_ts: int
    end_ts: int
    
    # Pre-filter check
    price_to_beat: float = 0.0
    current_price_at_start: float = 0.0
    price_distance: float = 0.0  # abs(current - price_to_beat)
    passed_prefilter: bool = False
    
    # Volatility
    volatility_proxy: float = 0.0
    mid_prices: List[float] = field(default_factory=list)
    
    # Quote lifecycle
    quotes_posted: int = 0
    cancels: int = 0
    replaces: int = 0
    quote_lifetimes: List[float] = field(default_factory=list)
    
    # Per-model tracking
    first_leg_fills: Dict[str, int] = field(default_factory=lambda: {"L": 0, "M": 0, "Q": 0})
    completed_pairs: Dict[str, int] = field(default_factory=lambda: {"L": 0, "M": 0, "Q": 0})
    edge_nets: Dict[str, List[float]] = field(default_factory=lambda: {"L": [], "M": [], "Q": []})
    
    # Timing
    first_fill_ts: float = 0.0
    completion_ts: float = 0.0
    time_to_complete: float = 0.0
    time_unhedged: float = 0.0
    max_unhedged_exposure: float = 0.0
    
    # Window mode
    mode: str = "normal"
    rescue_triggered: bool = False
    
    ticks_seen: int = 0
    
    def compute_volatility(self):
        if len(self.mid_prices) >= 2:
            self.volatility_proxy = max(self.mid_prices) - min(self.mid_prices)
    
    @property
    def avg_quote_lifetime(self) -> float:
        if self.quote_lifetimes:
            return sum(self.quote_lifetimes) / len(self.quote_lifetimes)
        return 0
    
    def to_dict(self) -> dict:
        return {
            "slug": self.slug,
            "passed_prefilter": self.passed_prefilter,
            "price_distance": self.price_distance,
            "volatility_proxy": self.volatility_proxy,
            "quotes_posted": self.quotes_posted,
            "cancels": self.cancels,
            "avg_quote_lifetime": self.avg_quote_lifetime,
            "first_leg_fills": self.first_leg_fills,
            "completed_pairs": self.completed_pairs,
            "edge_nets": {k: v for k, v in self.edge_nets.items()},
            "time_to_complete": self.time_to_complete,
            "time_unhedged": self.time_unhedged,
            "max_unhedged_exposure": self.max_unhedged_exposure,
            "mode": self.mode,
            "ticks_seen": self.ticks_seen,
        }


class EngineV4:
    """
    Production-ready maker pair-capture with queue realism.
    """
    
    def __init__(
        self,
        # Edge constraints
        edge_floor: float = 0.005,  # 0.5% minimum locked edge
        edge_floor_rescue: float = 0.0,  # Edge floor in rescue mode (can go to 0)
        initial_edge_target: float = 0.015,
        
        # Risk limits
        loss_cap: float = 0.50,  # Max loss allowed in rescue mode
        max_unhedged_qty: float = 20.0,
        
        # Quote lifecycle
        min_quote_lifetime_sec: float = 2.0,
        max_cancels_per_window: int = 20,
        
        # Model Q parameters
        model_m_persist_ticks: int = 3,
        model_q_persist_ticks: int = 5,
        model_q_fill_fraction: float = 0.5,  # Assume 50% of size fills
        model_q_size_depletion_ratio: float = 0.3,  # Require 30% of book size depleted
        
        # Pre-filter
        prefilter_max_price_distance: float = 0.10,  # Only trade if |current - beat| <= 10%
        
        # Sizing
        clip_size: float = 5.0,
        
        # Timing
        stop_new_entries_sec: float = 120,
        force_stop_sec: float = 30,
        
        # Run targets
        target_first_leg_fills: int = 200,
        min_first_leg_fills: int = 100,
        
        # Output
        output_dir: str = "pm_results_v4",
    ):
        self.edge_floor = edge_floor
        self.edge_floor_rescue = edge_floor_rescue
        self.initial_edge_target = initial_edge_target
        self.loss_cap = loss_cap
        self.max_unhedged_qty = max_unhedged_qty
        self.min_quote_lifetime_sec = min_quote_lifetime_sec
        self.max_cancels_per_window = max_cancels_per_window
        self.model_m_persist_ticks = model_m_persist_ticks
        self.model_q_persist_ticks = model_q_persist_ticks
        self.model_q_fill_fraction = model_q_fill_fraction
        self.model_q_size_depletion_ratio = model_q_size_depletion_ratio
        self.prefilter_max_price_distance = prefilter_max_price_distance
        self.clip_size = clip_size
        self.stop_new_entries_sec = stop_new_entries_sec
        self.force_stop_sec = force_stop_sec
        self.target_first_leg_fills = target_first_leg_fills
        self.min_first_leg_fills = min_first_leg_fills
        
        self.output_dir = Path(output_dir)
        self.output_dir.mkdir(exist_ok=True)
        
        self.fetcher = MarketFetcher()
        
        # State
        self.current_window: Optional[Window15Min] = None
        self.positions: Dict[str, Position] = {}  # Per model
        self.metrics: Optional[WindowMetrics] = None
        
        # Quotes
        self.quote_up: Optional[QuoteState] = None
        self.quote_down: Optional[QuoteState] = None
        
        # Completion state
        self.first_leg_side: Optional[Side] = None
        self.first_leg_price: float = 0.0
        self.first_leg_ts: float = 0.0
        self.window_mode: WindowMode = WindowMode.NORMAL
        
        # Results
        self.all_metrics: List[WindowMetrics] = []
        
        # Aggregate counters (for event-driven stopping)
        self.total_first_leg_fills_Q = 0
        
        # Logging
        self.log_file = None
        self._open_log()
    
    def _open_log(self):
        ts = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
        self.log_path = self.output_dir / f"trades_v4_{ts}.jsonl"
        self.log_file = open(self.log_path, "a")
    
    def _log(self, event: str, data: dict):
        entry = {"ts": time.time(), "event": event, **data}
        if self.log_file:
            self.log_file.write(json.dumps(entry) + "\n")
            self.log_file.flush()
    
    # =========================================================================
    # Pre-filter: Near 50/50 Windows Only
    # =========================================================================
    
    def _check_prefilter(self, tick: OrderBookTick) -> bool:
        """
        Only trade windows where price is near 50/50.
        
        Uses the first tick's mid as proxy for market state.
        Near-50/50 means both sides are priced around 0.50.
        """
        mid_up = (tick.bid_up + tick.ask_up) / 2 if tick.bid_up > 0 else tick.ask_up
        mid_down = (tick.bid_down + tick.ask_down) / 2 if tick.bid_down > 0 else tick.ask_down
        
        # Price distance from 50/50
        distance = abs(mid_up - 0.5) + abs(mid_down - 0.5)
        
        self.metrics.price_distance = distance
        self.metrics.current_price_at_start = mid_up
        
        passed = distance <= self.prefilter_max_price_distance
        self.metrics.passed_prefilter = passed
        
        if not passed:
            self._log("PREFILTER_SKIP", {
                "distance": distance,
                "mid_up": mid_up,
                "mid_down": mid_down,
                "threshold": self.prefilter_max_price_distance,
            })
            logger.info(f"SKIP: price_distance={distance:.3f} > {self.prefilter_max_price_distance}")
        
        return passed
    
    # =========================================================================
    # Quote Management with Lifecycle Constraints
    # =========================================================================
    
    def _can_cancel_quote(self, quote: QuoteState, now: float) -> bool:
        """Check if we can cancel (respects min lifetime)."""
        if quote.cancelled:
            return False
        
        lifetime = now - quote.posted_ts
        if lifetime < self.min_quote_lifetime_sec:
            return False
        
        if self.metrics.cancels >= self.max_cancels_per_window:
            return False
        
        return True
    
    def _cancel_quote(self, quote: QuoteState, now: float):
        """Cancel a quote and track metrics."""
        if not self._can_cancel_quote(quote, now):
            return False
        
        quote.cancelled = True
        lifetime = now - quote.posted_ts
        self.metrics.quote_lifetimes.append(lifetime)
        self.metrics.cancels += 1
        
        self._log("QUOTE_CANCEL", {
            "side": quote.side.value,
            "price": quote.price,
            "lifetime": lifetime,
        })
        
        return True
    
    def _post_quote(self, side: Side, price: float, tick: OrderBookTick) -> QuoteState:
        """Post a new quote."""
        book_size = tick.size_ask_up if side == Side.UP else tick.size_ask_down
        
        quote = QuoteState(
            side=side,
            price=price,
            size=self.clip_size,
            posted_ts=tick.ts,
            initial_book_size=book_size,
        )
        
        self.metrics.quotes_posted += 1
        
        self._log("QUOTE_POST", {
            "side": side.value,
            "price": price,
            "book_size": book_size,
        })
        
        return quote
    
    # =========================================================================
    # Fill Detection: Three Models (L, M, Q)
    # =========================================================================
    
    def _check_fills(self, tick: OrderBookTick):
        """Check fills under all three models."""
        
        for quote, current_ask, current_size in [
            (self.quote_up, tick.ask_up, tick.size_ask_up),
            (self.quote_down, tick.ask_down, tick.size_ask_down),
        ]:
            if not quote or quote.cancelled:
                continue
            
            is_cross = current_ask <= quote.price
            
            # Model L: instant cross-through
            if is_cross and not quote.filled_L:
                quote.filled_L = True
                self._record_fill(quote.side, quote.price, tick.ts, FillModel.L, tick=tick)
            
            # Model M: persistent cross-through
            if is_cross:
                quote.cross_ticks += 1
            else:
                quote.cross_ticks = 0
            
            if quote.cross_ticks >= self.model_m_persist_ticks and not quote.filled_M:
                quote.filled_M = True
                self._record_fill(quote.side, quote.price, tick.ts, FillModel.M, tick=tick)
            
            # Model Q: persistent + size depletion + partial fill
            if quote.cross_ticks >= self.model_q_persist_ticks and not quote.filled_Q:
                # Check size depletion
                size_depleted = quote.initial_book_size - current_size
                depletion_ratio = size_depleted / quote.initial_book_size if quote.initial_book_size > 0 else 0
                
                if depletion_ratio >= self.model_q_size_depletion_ratio:
                    quote.filled_Q = True
                    # Partial fill
                    partial_qty = quote.size * self.model_q_fill_fraction
                    quote.partial_fill_qty = partial_qty
                    self._record_fill(quote.side, quote.price, tick.ts, FillModel.Q, partial_qty, tick=tick)
    
    def _record_fill(self, side: Side, price: float, ts: float, model: FillModel, qty: float = None, tick: OrderBookTick = None):
        """Record fill in the appropriate position."""
        qty = qty or self.clip_size
        
        pos = self.positions[model.value]
        was_empty = pos.q_up == 0 and pos.q_down == 0
        
        pos.record_fill(side, qty, price)
        
        if was_empty:
            # First leg fill
            self.metrics.first_leg_fills[model.value] += 1
            
            if model == FillModel.Q:
                self.total_first_leg_fills_Q += 1
            
            if self.first_leg_ts == 0:
                self.first_leg_ts = ts
                self.first_leg_side = side
                self.first_leg_price = price
                self.metrics.first_fill_ts = ts
            
            # Compute what's needed to complete at target edge
            max_completion_price = 1.0 - self.edge_floor - price
            if side == Side.UP:
                other_ask = tick.ask_down if tick else None
                other_bid = tick.bid_down if tick else None
            else:
                other_ask = tick.ask_up if tick else None
                other_bid = tick.bid_up if tick else None
            
            can_complete_at_edge = other_ask <= max_completion_price if other_ask else False
            
            self._log("FIRST_LEG_FILL", {
                "model": model.value,
                "side": side.value,
                "price": price,
                "qty": qty,
                "max_completion_at_edge": max_completion_price,
                "other_side_ask": other_ask,
                "other_side_bid": other_bid,
                "can_complete_at_edge": can_complete_at_edge,
                "edge_floor": self.edge_floor,
            })
            
            other_ask_str = f"{other_ask:.4f}" if other_ask is not None else "N/A"
            logger.info(f"FIRST LEG ({model.value}): {side.value} @ {price:.4f}, "
                       f"max_comp={max_completion_price:.4f}, other_ask={other_ask_str}, "
                       f"can_complete={can_complete_at_edge}")
        
        # Check completion
        if pos.hedged_qty > 0:
            self.metrics.completed_pairs[model.value] += 1
            self.metrics.edge_nets[model.value].append(pos.edge_net)
            
            if self.metrics.completion_ts == 0:
                self.metrics.completion_ts = ts
                self.metrics.time_to_complete = ts - self.metrics.first_fill_ts
            
            # Compute detailed debug info
            q_matched = pos.hedged_qty
            cost_total = pos.cost_up + pos.cost_down
            payout_locked = q_matched  # $1 per matched pair
            edge_locked = payout_locked - cost_total
            pair_cost = pos.avg_up + pos.avg_down
            
            # Determine first/completion sides and prices
            if self.first_leg_side == Side.UP:
                side_first, p_first = "up", self.first_leg_price
                side_comp, p_comp = "down", pos.avg_down
            else:
                side_first, p_first = "down", self.first_leg_price
                side_comp, p_comp = "up", pos.avg_up
            
            # Compute completion cap (what was allowed)
            completion_cap = 1.0 - self.edge_floor - p_first
            if self.window_mode == WindowMode.RESCUE:
                completion_cap_rescue = 1.0 - self.edge_floor_rescue - p_first
            else:
                completion_cap_rescue = completion_cap
            
            invariant_ok = p_comp <= completion_cap_rescue + 1e-9
            
            self._log("PAIR_COMPLETE", {
                "model": model.value,
                "window_mode": self.window_mode.value,
                "side_first": side_first,
                "p_first": p_first,
                "q_first": pos.q_up if self.first_leg_side == Side.UP else pos.q_down,
                "side_comp": side_comp,
                "p_comp": p_comp,
                "q_comp": pos.q_down if self.first_leg_side == Side.UP else pos.q_up,
                "q_matched": q_matched,
                "cost_total": cost_total,
                "payout_locked": payout_locked,
                "edge_locked": edge_locked,
                "pair_cost": pair_cost,
                "completion_cap_normal": 1.0 - self.edge_floor - p_first,
                "completion_cap_used": completion_cap_rescue,
                "invariant_ok": invariant_ok,
                "edge_net_per_pair": pos.edge_net,
                "time_to_complete": self.metrics.time_to_complete,
            })
            
            # Log with full precision
            logger.info(f"PAIR COMPLETE ({model.value}): "
                       f"mode={self.window_mode.value}, "
                       f"p_first={p_first:.4f}, p_comp={p_comp:.4f}, "
                       f"pair_cost={pair_cost:.4f}, "
                       f"edge_locked=${edge_locked:.4f}, "
                       f"invariant_ok={invariant_ok}")
    
    # =========================================================================
    # Completion Logic with Hard Invariant
    # =========================================================================
    
    def _compute_max_completion_price(self) -> float:
        """
        Hard invariant: completion_bid <= 1 - edge_floor - first_leg_price
        """
        if self.window_mode == WindowMode.RESCUE:
            edge = self.edge_floor_rescue
        else:
            edge = self.edge_floor
        
        return 1.0 - edge - self.first_leg_price
    
    def _should_enter_rescue_mode(self, tick: OrderBookTick) -> Tuple[bool, dict]:
        """
        Check if we should switch to rescue mode.
        
        Enter rescue if max_completion <= best_ask_other (can't fill at target edge even crossing).
        
        Returns (should_rescue, debug_info).
        """
        if self.window_mode != WindowMode.NORMAL:
            return False, {}
        
        max_price = self._compute_max_completion_price()
        
        if self.first_leg_side == Side.UP:
            other_best_ask = tick.ask_down
            other_best_bid = tick.bid_down
        else:
            other_best_ask = tick.ask_up
            other_best_bid = tick.bid_up
        
        debug_info = {
            "max_completion_price": max_price,
            "other_best_ask": other_best_ask,
            "other_best_bid": other_best_bid,
            "first_leg_price": self.first_leg_price,
            "edge_floor": self.edge_floor,
        }
        
        # Only rescue if we can't even CROSS the spread at target edge
        # (i.e., max_price < best_ask means we'd have to pay more than allowed)
        if max_price < other_best_ask:
            debug_info["reason"] = f"max_price({max_price:.4f}) < best_ask({other_best_ask:.4f})"
            return True, debug_info
        
        return False, debug_info
    
    def _check_rescue_feasibility(self, tick: OrderBookTick) -> bool:
        """Check if rescue is within loss_cap."""
        if self.first_leg_side == Side.UP:
            rescue_price = tick.ask_down
        else:
            rescue_price = tick.ask_up
        
        rescue_cost = self.first_leg_price + rescue_price
        rescue_loss = max(0, rescue_cost - 1.0) * self.clip_size
        
        return rescue_loss <= self.loss_cap
    
    def _update_quotes(self, tick: OrderBookTick):
        """Update quotes based on current state."""
        now = tick.ts
        remaining = tick.seconds_remaining
        
        # Force stop near end
        if remaining <= self.force_stop_sec:
            if self.quote_up:
                self._cancel_quote(self.quote_up, now)
            if self.quote_down:
                self._cancel_quote(self.quote_down, now)
            self.window_mode = WindowMode.STOPPED
            return
        
        # Track unhedged time
        if self.first_leg_ts > 0 and self.positions["Q"].hedged_qty == 0:
            self.metrics.time_unhedged += 0.5  # Approximate per tick
            self.metrics.max_unhedged_exposure = max(
                self.metrics.max_unhedged_exposure,
                self.positions["Q"].max_loss
            )
        
        # If stopped, do nothing
        if self.window_mode == WindowMode.STOPPED:
            return
        
        # If no first leg yet, post initial quotes
        if not self.first_leg_side:
            if remaining > self.stop_new_entries_sec:
                self._post_initial_quotes(tick)
            return
        
        # We have a first leg - work on completion
        self._update_completion_quote(tick, remaining)
    
    def _post_initial_quotes(self, tick: OrderBookTick):
        """Post initial two-sided quotes."""
        mid_up = (tick.bid_up + tick.ask_up) / 2 if tick.bid_up > 0 else tick.ask_up - 0.01
        mid_down = (tick.bid_down + tick.ask_down) / 2 if tick.bid_down > 0 else tick.ask_down - 0.01
        
        # Bids below mid, constrained by edge target
        delta = 0.01
        bid_up = mid_up - delta
        bid_down = mid_down - delta
        
        max_sum = 1.0 - self.initial_edge_target
        if bid_up + bid_down > max_sum:
            excess = bid_up + bid_down - max_sum
            bid_up -= excess / 2
            bid_down -= excess / 2
        
        if not self.quote_up or self.quote_up.cancelled:
            self.quote_up = self._post_quote(Side.UP, max(0.01, bid_up), tick)
        
        if not self.quote_down or self.quote_down.cancelled:
            self.quote_down = self._post_quote(Side.DOWN, max(0.01, bid_down), tick)
    
    def _update_completion_quote(self, tick: OrderBookTick, remaining: float):
        """Update the completion-side quote with hard invariant."""
        now = tick.ts
        
        # Check if we should enter rescue mode
        should_rescue, rescue_debug = self._should_enter_rescue_mode(tick)
        if should_rescue:
            if self._check_rescue_feasibility(tick):
                self.window_mode = WindowMode.RESCUE
                self.metrics.rescue_triggered = True
                self._log("RESCUE_MODE_ENTER", {
                    "reason": rescue_debug.get("reason", "completion_impossible_at_edge"),
                    **rescue_debug,
                })
                logger.warning(f"Entering RESCUE mode: {rescue_debug.get('reason', '')}")
            else:
                # Can't rescue within loss cap - stop window
                self.window_mode = WindowMode.STOPPED
                self.metrics.mode = "stopped_no_rescue"
                self._log("WINDOW_STOP", {
                    "reason": "rescue_exceeds_loss_cap",
                    **rescue_debug,
                })
                logger.warning("STOPPED: rescue exceeds loss_cap")
                return
        
        # Compute max completion price (HARD INVARIANT)
        max_completion = self._compute_max_completion_price()
        
        # Time-based aggressiveness
        aggressiveness = 1.0 - (remaining / 900)
        
        # Conservative vs aggressive bid
        if self.first_leg_side == Side.UP:
            mid = (tick.bid_down + tick.ask_down) / 2 if tick.bid_down > 0 else tick.ask_down - 0.01
            target_quote = self.quote_down
            target_side = Side.DOWN
        else:
            mid = (tick.bid_up + tick.ask_up) / 2 if tick.bid_up > 0 else tick.ask_up - 0.01
            target_quote = self.quote_up
            target_side = Side.UP
        
        conservative = mid - 0.02
        aggressive = max_completion  # Never exceed this
        
        new_price = conservative + (aggressive - conservative) * aggressiveness
        new_price = min(new_price, max_completion)  # HARD CAP
        new_price = max(0.01, new_price)
        
        # Check if we should update (respects lifecycle)
        if target_quote and not target_quote.cancelled:
            if new_price > target_quote.price + 0.002:  # Meaningful improvement
                if self._can_cancel_quote(target_quote, now):
                    self._cancel_quote(target_quote, now)
                    if target_side == Side.UP:
                        self.quote_up = self._post_quote(Side.UP, new_price, tick)
                    else:
                        self.quote_down = self._post_quote(Side.DOWN, new_price, tick)
                    self.metrics.replaces += 1
        else:
            # No quote, post new
            if target_side == Side.UP:
                self.quote_up = self._post_quote(Side.UP, new_price, tick)
            else:
                self.quote_down = self._post_quote(Side.DOWN, new_price, tick)
    
    # =========================================================================
    # Window Management
    # =========================================================================
    
    def _start_window(self, slug: str):
        """Initialize new window."""
        if self.metrics:
            self._finalize_window()
            self.all_metrics.append(self.metrics)
        
        self.current_window = self.fetcher.fetch_market_by_slug(slug)
        if not self.current_window:
            return False
        
        # Reset state
        self.positions = {m.value: Position() for m in FillModel}
        self.metrics = WindowMetrics(slug=slug, start_ts=self.current_window.start_ts, end_ts=self.current_window.end_ts)
        self.quote_up = None
        self.quote_down = None
        self.first_leg_side = None
        self.first_leg_price = 0
        self.first_leg_ts = 0
        self.window_mode = WindowMode.NORMAL
        
        self._log("WINDOW_START", {"slug": slug})
        logger.info(f"Started: {slug}")
        return True
    
    def _finalize_window(self):
        """Finalize window metrics."""
        self.metrics.compute_volatility()
        self.metrics.mode = self.window_mode.value
        self._log("WINDOW_END", self.metrics.to_dict())
    
    def process_tick(self, tick: OrderBookTick):
        """Process single tick."""
        if not self.metrics:
            return
        
        self.metrics.ticks_seen += 1
        
        # Track mid for volatility
        mid = (tick.ask_up + tick.ask_down) / 2
        self.metrics.mid_prices.append(mid)
        
        # Pre-filter check (only on first tick)
        if self.metrics.ticks_seen == 1:
            if not self._check_prefilter(tick):
                self.window_mode = WindowMode.STOPPED
                return
        
        if self.window_mode == WindowMode.STOPPED:
            return
        
        # Check fills
        self._check_fills(tick)
        
        # Update quotes
        self._update_quotes(tick)
    
    # =========================================================================
    # Run Loop
    # =========================================================================
    
    def run_until_fills(self, max_duration_minutes: int = 600):
        """
        Run until target first-leg fills reached.
        
        Event-driven: stops at target_first_leg_fills, not time.
        """
        print("\n" + "="*70)
        print("Engine V4 - Production Maker Pair-Capture")
        print("="*70)
        print(f"Target first-leg fills (Model Q): {self.target_first_leg_fills}")
        print(f"Min for decision: {self.min_first_leg_fills}")
        print(f"Pre-filter: price_distance <= {self.prefilter_max_price_distance}")
        print(f"Edge floor: {self.edge_floor*100:.1f}%")
        print(f"Model Q: persist={self.model_q_persist_ticks}, depletion={self.model_q_size_depletion_ratio}")
        print(f"Output: {self.log_path}")
        print("="*70 + "\n")
        
        start_time = time.time()
        max_sec = max_duration_minutes * 60
        poll_sec = 0.5
        
        try:
            while True:
                # Check stop conditions
                if self.total_first_leg_fills_Q >= self.target_first_leg_fills:
                    print(f"\nTarget reached: {self.total_first_leg_fills_Q} first-leg fills")
                    break
                
                if time.time() - start_time >= max_sec:
                    print(f"\nMax duration reached")
                    break
                
                current_slug = get_current_window_slug()
                
                if not self.current_window or self.current_window.slug != current_slug:
                    self._start_window(current_slug)
                
                if not self.current_window or self.current_window.is_finished():
                    time.sleep(poll_sec)
                    continue
                
                tick = self.fetcher.fetch_tick(self.current_window)
                if tick and tick.ask_up > 0:
                    self.process_tick(tick)
                    
                    if self.metrics and self.metrics.ticks_seen % 30 == 0:
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
        print(f"[{ts}] W{len(self.all_metrics)+1} | "
              f"{tick.seconds_remaining:.0f}s | "
              f"Fills(Q):{self.total_first_leg_fills_Q}/{self.target_first_leg_fills} | "
              f"Mode:{self.window_mode.value}")
    
    def _print_summary(self):
        """Print comprehensive summary with L/M/Q band."""
        print("\n" + "="*70)
        print("V4 SUMMARY - THREE-MODEL BAND")
        print("="*70)
        
        print(f"\nWindows: {len(self.all_metrics)}")
        passed = sum(1 for m in self.all_metrics if m.passed_prefilter)
        print(f"Passed pre-filter: {passed}")
        
        # Per-model results
        for model in ["L", "M", "Q"]:
            total_first = sum(m.first_leg_fills.get(model, 0) for m in self.all_metrics)
            total_complete = sum(m.completed_pairs.get(model, 0) for m in self.all_metrics)
            
            print(f"\n--- Model {model} ---")
            print(f"  First-leg fills: {total_first}")
            print(f"  Completed pairs: {total_complete}")
            
            if total_first > 0:
                p_complete = total_complete / total_first * 100
                print(f"  P(complete|first): {p_complete:.1f}%")
            
            # Edge distribution
            all_edges = []
            for m in self.all_metrics:
                all_edges.extend(m.edge_nets.get(model, []))
            
            if all_edges:
                median_edge = statistics.median(all_edges) * 100
                p10_edge = sorted(all_edges)[len(all_edges)//10] * 100 if len(all_edges) >= 10 else min(all_edges) * 100
                print(f"  Edge median: {median_edge:.2f} cents")
                print(f"  Edge p10: {p10_edge:.2f} cents")
        
        # Stratification by volatility
        print(f"\n--- Stratified by Volatility ---")
        high_vol = [m for m in self.all_metrics if m.volatility_proxy > 0.02]
        low_vol = [m for m in self.all_metrics if m.volatility_proxy <= 0.02]
        
        for name, subset in [("High-vol (>2%)", high_vol), ("Low-vol (<=2%)", low_vol)]:
            if subset:
                fills_Q = sum(m.first_leg_fills.get("Q", 0) for m in subset)
                complete_Q = sum(m.completed_pairs.get("Q", 0) for m in subset)
                p_complete = complete_Q / fills_Q * 100 if fills_Q > 0 else 0
                print(f"  {name}: {len(subset)} windows, fills={fills_Q}, P(complete)={p_complete:.1f}%")
        
        # Decision
        print(f"\n--- DECISION ---")
        total_first_Q = sum(m.first_leg_fills.get("Q", 0) for m in self.all_metrics)
        total_complete_Q = sum(m.completed_pairs.get("Q", 0) for m in self.all_metrics)
        
        if total_first_Q < self.min_first_leg_fills:
            print(f"  INCONCLUSIVE: Only {total_first_Q} first-leg fills (need {self.min_first_leg_fills}+)")
        else:
            p_complete_Q = total_complete_Q / total_first_Q * 100
            
            all_edges_Q = []
            for m in self.all_metrics:
                all_edges_Q.extend(m.edge_nets.get("Q", []))
            
            median_edge_Q = statistics.median(all_edges_Q) * 100 if all_edges_Q else 0
            
            if p_complete_Q < 20:
                print(f"  KILL: P(complete|first) = {p_complete_Q:.1f}% < 20% under Model Q")
            elif p_complete_Q > 50 and median_edge_Q > 0.5:
                print(f"  PROMISING: P(complete|first) = {p_complete_Q:.1f}% > 50%, edge = {median_edge_Q:.2f}c")
            else:
                print(f"  MARGINAL: P(complete|first) = {p_complete_Q:.1f}%, edge = {median_edge_Q:.2f}c")
    
    def _save_results(self):
        """Save results."""
        path = self.output_dir / f"results_v4_{datetime.now(timezone.utc).strftime('%Y%m%d_%H%M%S')}.json"
        
        summary = {}
        for model in ["L", "M", "Q"]:
            total_first = sum(m.first_leg_fills.get(model, 0) for m in self.all_metrics)
            total_complete = sum(m.completed_pairs.get(model, 0) for m in self.all_metrics)
            all_edges = []
            for m in self.all_metrics:
                all_edges.extend(m.edge_nets.get(model, []))
            
            summary[model] = {
                "first_leg_fills": total_first,
                "completed_pairs": total_complete,
                "p_complete_given_first": total_complete / total_first if total_first > 0 else 0,
                "edge_median": statistics.median(all_edges) if all_edges else 0,
                "edge_p10": sorted(all_edges)[len(all_edges)//10] if len(all_edges) >= 10 else (min(all_edges) if all_edges else 0),
            }
        
        data = {
            "params": {
                "edge_floor": self.edge_floor,
                "model_q_persist_ticks": self.model_q_persist_ticks,
                "model_q_fill_fraction": self.model_q_fill_fraction,
                "prefilter_max_price_distance": self.prefilter_max_price_distance,
            },
            "summary": summary,
            "windows": [m.to_dict() for m in self.all_metrics],
        }
        
        with open(path, "w") as f:
            json.dump(data, f, indent=2)
        
        print(f"\nResults: {path}")


if __name__ == "__main__":
    import argparse
    
    logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(levelname)s | %(message)s", datefmt="%H:%M:%S")
    
    parser = argparse.ArgumentParser(description="Engine V4 - Production Maker Pair-Capture")
    parser.add_argument("--target-fills", type=int, default=200, help="Target first-leg fills")
    parser.add_argument("--min-fills", type=int, default=100, help="Min fills for decision")
    parser.add_argument("--max-hours", type=int, default=10, help="Max run time in hours")
    parser.add_argument("--edge-floor", type=float, default=0.005, help="Min edge (0.5%)")
    parser.add_argument("--prefilter", type=float, default=0.10, help="Max price distance for prefilter")
    
    args = parser.parse_args()
    
    engine = EngineV4(
        edge_floor=args.edge_floor,
        target_first_leg_fills=args.target_fills,
        min_first_leg_fills=args.min_fills,
        prefilter_max_price_distance=args.prefilter,
    )
    engine.run_until_fills(max_duration_minutes=args.max_hours * 60)

