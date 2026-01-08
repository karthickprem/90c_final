"""
Feasibility Analyzer - Quantify edge capture viability

Modes:
1. Shadow Sample: Collect orderbook data, compute feasibility rates without trading
2. Strict Arb: Only enter if completion is immediately feasible at target edge
3. Maker Wait: Wait for maker completion after first leg fill

Outputs:
- Feasibility curve: feasible_rate per edge_floor
- Gap distribution: p50/p90/p99 of (other_ask - cap) in cents
- Hard kill/proceed decision data
"""

import logging
import time
import json
import statistics
from pathlib import Path
from datetime import datetime, timezone
from typing import Optional, List, Dict, Tuple
from dataclasses import dataclass, field
from collections import defaultdict

from .market_v2 import (
    MarketFetcher, 
    Window15Min, 
    OrderBookTick,
    get_current_window_slug,
    get_next_window_slug,
)

logger = logging.getLogger(__name__)


# Edge floors to test (0%, 0.1%, 0.2%, 0.3%, 0.5%)
EDGE_FLOORS = [0.0, 0.001, 0.002, 0.003, 0.005]


@dataclass
class FeasibilityStats:
    """Stats for a single edge floor."""
    edge_floor: float
    samples: int = 0
    feasible_up_first: int = 0  # Can complete if Up fills first
    feasible_down_first: int = 0  # Can complete if Down fills first
    gaps_up_first: List[float] = field(default_factory=list)  # (ask_down - cap) in cents
    gaps_down_first: List[float] = field(default_factory=list)  # (ask_up - cap) in cents
    
    @property
    def feasible_rate_up(self) -> float:
        return self.feasible_up_first / self.samples if self.samples > 0 else 0
    
    @property
    def feasible_rate_down(self) -> float:
        return self.feasible_down_first / self.samples if self.samples > 0 else 0
    
    @property
    def feasible_rate_avg(self) -> float:
        return (self.feasible_rate_up + self.feasible_rate_down) / 2
    
    def gap_percentiles(self, side: str) -> Dict[str, float]:
        """Compute p50, p90, p99 of gaps in cents."""
        gaps = self.gaps_up_first if side == "up" else self.gaps_down_first
        if not gaps:
            return {"p50": 0, "p90": 0, "p99": 0}
        
        sorted_gaps = sorted(gaps)
        n = len(sorted_gaps)
        return {
            "p50": sorted_gaps[int(n * 0.50)] if n > 0 else 0,
            "p90": sorted_gaps[int(n * 0.90)] if n > 1 else sorted_gaps[-1],
            "p99": sorted_gaps[int(n * 0.99)] if n > 2 else sorted_gaps[-1],
        }
    
    def to_dict(self) -> dict:
        up_pct = self.gap_percentiles("up")
        down_pct = self.gap_percentiles("down")
        return {
            "edge_floor": self.edge_floor,
            "edge_floor_pct": f"{self.edge_floor * 100:.1f}%",
            "samples": self.samples,
            "feasible_rate_up_first": self.feasible_rate_up,
            "feasible_rate_down_first": self.feasible_rate_down,
            "feasible_rate_avg": self.feasible_rate_avg,
            "gap_up_first_cents": {
                "p50": up_pct["p50"] * 100,
                "p90": up_pct["p90"] * 100,
                "p99": up_pct["p99"] * 100,
            },
            "gap_down_first_cents": {
                "p50": down_pct["p50"] * 100,
                "p90": down_pct["p90"] * 100,
                "p99": down_pct["p99"] * 100,
            },
        }


@dataclass
class ShadowSampleResult:
    """Complete shadow sampling result."""
    start_ts: float = 0
    end_ts: float = 0
    duration_minutes: float = 0
    windows_sampled: int = 0
    total_ticks: int = 0
    stats_by_floor: Dict[float, FeasibilityStats] = field(default_factory=dict)
    
    # Raw book stats
    ask_sums: List[float] = field(default_factory=list)  # ask_up + ask_down
    bid_sums: List[float] = field(default_factory=list)  # bid_up + bid_down
    spreads_up: List[float] = field(default_factory=list)
    spreads_down: List[float] = field(default_factory=list)
    
    def to_dict(self) -> dict:
        ask_sum_pct = self._percentiles(self.ask_sums)
        bid_sum_pct = self._percentiles(self.bid_sums)
        spread_up_pct = self._percentiles(self.spreads_up)
        spread_down_pct = self._percentiles(self.spreads_down)
        
        return {
            "run_params": {
                "start_ts": self.start_ts,
                "end_ts": self.end_ts,
                "duration_minutes": self.duration_minutes,
                "windows_sampled": self.windows_sampled,
                "total_ticks": self.total_ticks,
            },
            "book_stats": {
                "ask_sum": {"p50": ask_sum_pct["p50"], "p90": ask_sum_pct["p90"], "min": ask_sum_pct["min"]},
                "bid_sum": {"p50": bid_sum_pct["p50"], "p90": bid_sum_pct["p90"], "max": bid_sum_pct["max"]},
                "spread_up_cents": {"p50": spread_up_pct["p50"] * 100, "p90": spread_up_pct["p90"] * 100},
                "spread_down_cents": {"p50": spread_down_pct["p50"] * 100, "p90": spread_down_pct["p90"] * 100},
            },
            "feasibility_by_edge_floor": {
                f"{ef*100:.1f}%": stats.to_dict() 
                for ef, stats in sorted(self.stats_by_floor.items())
            },
        }
    
    def _percentiles(self, data: List[float]) -> dict:
        if not data:
            return {"p50": 0, "p90": 0, "min": 0, "max": 0}
        s = sorted(data)
        n = len(s)
        return {
            "p50": s[int(n * 0.50)],
            "p90": s[int(n * 0.90)] if n > 1 else s[-1],
            "min": s[0],
            "max": s[-1],
        }


class FeasibilityAnalyzer:
    """
    Shadow sample the orderbook to build feasibility curves.
    No trading - just observing.
    """
    
    def __init__(
        self,
        edge_floors: List[float] = None,
        output_dir: str = "pm_results_v4",
        sample_interval_ms: float = 300,  # Sample every 300ms
    ):
        self.edge_floors = edge_floors or EDGE_FLOORS
        self.output_dir = Path(output_dir)
        self.output_dir.mkdir(exist_ok=True)
        self.sample_interval_ms = sample_interval_ms
        
        self.fetcher = MarketFetcher()
        self.result = ShadowSampleResult()
        
        # Initialize stats for each edge floor
        for ef in self.edge_floors:
            self.result.stats_by_floor[ef] = FeasibilityStats(edge_floor=ef)
        
        # Log file
        ts_str = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
        self.log_path = self.output_dir / f"shadow_sample_{ts_str}.jsonl"
        self.log_file = None
    
    def _log(self, event: str, data: dict):
        if self.log_file:
            record = {"ts": time.time(), "event": event, **data}
            self.log_file.write(json.dumps(record) + "\n")
            self.log_file.flush()
    
    def _process_tick(self, tick: OrderBookTick):
        """Process a single tick and update all feasibility stats."""
        # Sanity check - skip bad ticks (empty book, corrupt data)
        if tick.ask_up <= 0 or tick.ask_down <= 0:
            logger.debug(f"Skip bad tick: ask_up={tick.ask_up}, ask_down={tick.ask_down}")
            return
        if tick.bid_up <= 0 or tick.bid_down <= 0:
            logger.debug(f"Skip bad tick: bid_up={tick.bid_up}, bid_down={tick.bid_down}")
            return
        if tick.ask_up <= tick.bid_up or tick.ask_down <= tick.bid_down:
            logger.debug(f"Skip inverted book: ask_up={tick.ask_up}, bid_up={tick.bid_up}")
            return
        
        self.result.total_ticks += 1
        
        # Record book stats
        ask_sum = tick.ask_up + tick.ask_down
        bid_sum = tick.bid_up + tick.bid_down
        spread_up = tick.ask_up - tick.bid_up
        spread_down = tick.ask_down - tick.bid_down
        
        self.result.ask_sums.append(ask_sum)
        self.result.bid_sums.append(bid_sum)
        self.result.spreads_up.append(spread_up)
        self.result.spreads_down.append(spread_down)
        
        # For each edge floor, compute feasibility
        for ef in self.edge_floors:
            stats = self.result.stats_by_floor[ef]
            stats.samples += 1
            
            # If Up fills first at bid_up, can we complete at ask_down <= cap?
            # Use bid as "where we'd fill if posting"
            p_up = tick.bid_up
            cap_for_up = 1.0 - ef - p_up
            gap_up = tick.ask_down - cap_for_up  # positive = infeasible
            stats.gaps_up_first.append(gap_up)
            if tick.ask_down <= cap_for_up:
                stats.feasible_up_first += 1
            
            # If Down fills first at bid_down, can we complete at ask_up <= cap?
            p_down = tick.bid_down
            cap_for_down = 1.0 - ef - p_down
            gap_down = tick.ask_up - cap_for_down
            stats.gaps_down_first.append(gap_down)
            if tick.ask_up <= cap_for_down:
                stats.feasible_down_first += 1
        
        # Log every 100 ticks
        if self.result.total_ticks % 100 == 0:
            self._log("SAMPLE", {
                "tick": self.result.total_ticks,
                "ask_sum": ask_sum,
                "bid_sum": bid_sum,
                "ask_up": tick.ask_up,
                "ask_down": tick.ask_down,
                "bid_up": tick.bid_up,
                "bid_down": tick.bid_down,
            })
    
    def run_shadow_sample(self, max_minutes: float = 30, max_windows: int = 100):
        """
        Run shadow sampling for specified duration.
        No trading - just observe and compute feasibility.
        """
        print("\n" + "=" * 70)
        print("SHADOW SAMPLE MODE - Feasibility Analysis")
        print("=" * 70)
        print(f"Edge floors to test: {[f'{ef*100:.1f}%' for ef in self.edge_floors]}")
        print(f"Sample interval: {self.sample_interval_ms}ms")
        print(f"Max duration: {max_minutes} minutes")
        print(f"Output: {self.log_path}")
        print("=" * 70)
        
        self.result.start_ts = time.time()
        deadline = self.result.start_ts + max_minutes * 60
        windows_seen = 0
        current_window = None
        
        self.log_file = open(self.log_path, "w", encoding="utf-8")
        
        try:
            while time.time() < deadline and windows_seen < max_windows:
                # Get current window
                slug = get_current_window_slug()
                if slug != current_window:
                    current_window = slug
                    windows_seen += 1
                    self.result.windows_sampled = windows_seen
                    
                    # Fetch market
                    window = self.fetcher.fetch_market_by_slug(slug)
                    if not window:
                        logger.warning(f"Could not fetch market for {slug}")
                        time.sleep(5)
                        continue
                    
                    logger.info(f"Window {windows_seen}: {slug}")
                    self._log("WINDOW_START", {"slug": slug, "window": windows_seen})
                
                # Fetch tick
                tick = self.fetcher.fetch_tick(window)
                if tick:
                    self._process_tick(tick)
                    
                    # Progress every 50 ticks
                    if self.result.total_ticks % 50 == 0:
                        elapsed = (time.time() - self.result.start_ts) / 60
                        rate_05 = self.result.stats_by_floor[0.005].feasible_rate_avg * 100
                        rate_02 = self.result.stats_by_floor[0.002].feasible_rate_avg * 100
                        print(f"[{elapsed:.1f}m] Ticks: {self.result.total_ticks} | "
                              f"Feasible@0.5%: {rate_05:.1f}% | @0.2%: {rate_02:.1f}%")
                
                # Wait
                time.sleep(self.sample_interval_ms / 1000)
        
        except KeyboardInterrupt:
            logger.info("Interrupted by user")
        finally:
            self.log_file.close()
        
        self.result.end_ts = time.time()
        self.result.duration_minutes = (self.result.end_ts - self.result.start_ts) / 60
        
        # Save and print results
        self._save_results()
        self._print_summary()
    
    def _save_results(self):
        """Save results to JSON."""
        path = self.output_dir / f"feasibility_{datetime.now(timezone.utc).strftime('%Y%m%d_%H%M%S')}.json"
        
        with open(path, "w", encoding="utf-8") as f:
            json.dump(self.result.to_dict(), f, indent=2)
        
        print(f"\nResults saved: {path}")
    
    def _print_summary(self):
        """Print summary with decision guidance."""
        print("\n" + "=" * 70)
        print("FEASIBILITY ANALYSIS SUMMARY")
        print("=" * 70)
        
        print(f"\nDuration: {self.result.duration_minutes:.1f} minutes")
        print(f"Windows: {self.result.windows_sampled}")
        print(f"Ticks sampled: {self.result.total_ticks}")
        
        # Book stats
        if self.result.ask_sums:
            ask_sum_p50 = sorted(self.result.ask_sums)[len(self.result.ask_sums)//2]
            ask_sum_min = min(self.result.ask_sums)
            print(f"\nAsk Sum (ask_up + ask_down):")
            print(f"  p50: {ask_sum_p50:.4f}  min: {ask_sum_min:.4f}")
            if ask_sum_min < 0.998:
                print(f"  NOTE: Saw ask_sum < 0.998 ({ask_sum_min:.4f}) - potential instant arb!")
            else:
                print(f"  No instant arb opportunities (min ask_sum >= 0.998)")
        
        # Feasibility by edge floor
        print("\n" + "-" * 70)
        print("FEASIBILITY BY EDGE FLOOR")
        print("-" * 70)
        print(f"{'Edge Floor':<12} {'Feasible %':<12} {'Gap p50 (c)':<12} {'Gap p90 (c)':<12} {'Gap p99 (c)':<12}")
        print("-" * 70)
        
        for ef in sorted(self.edge_floors):
            stats = self.result.stats_by_floor[ef]
            # Use worst-case (up_first and down_first averaged)
            gap_up = stats.gap_percentiles("up")
            gap_down = stats.gap_percentiles("down")
            avg_p50 = (gap_up["p50"] + gap_down["p50"]) / 2 * 100
            avg_p90 = (gap_up["p90"] + gap_down["p90"]) / 2 * 100
            avg_p99 = (gap_up["p99"] + gap_down["p99"]) / 2 * 100
            
            print(f"{ef*100:.1f}%         {stats.feasible_rate_avg*100:>8.1f}%    "
                  f"{avg_p50:>8.2f}c     {avg_p90:>8.2f}c     {avg_p99:>8.2f}c")
        
        # Decision
        print("\n" + "=" * 70)
        print("DECISION GUIDANCE")
        print("=" * 70)
        
        rate_05 = self.result.stats_by_floor[0.005].feasible_rate_avg
        rate_03 = self.result.stats_by_floor[0.003].feasible_rate_avg
        rate_02 = self.result.stats_by_floor[0.002].feasible_rate_avg
        rate_01 = self.result.stats_by_floor[0.001].feasible_rate_avg
        rate_00 = self.result.stats_by_floor[0.0].feasible_rate_avg
        
        gap_05_p90 = (
            self.result.stats_by_floor[0.005].gap_percentiles("up")["p90"] +
            self.result.stats_by_floor[0.005].gap_percentiles("down")["p90"]
        ) / 2 * 100
        
        if rate_05 < 0.01 and gap_05_p90 > 1.0:
            print(f"  KILL 0.5% FLOOR: feasible={rate_05*100:.1f}% < 1%, p90 gap={gap_05_p90:.2f}c > 1c")
        elif rate_05 < 0.05:
            print(f"  WARNING: 0.5% floor barely feasible ({rate_05*100:.1f}%)")
        
        if rate_02 > 0.20:
            print(f"  CONSIDER 0.2% FLOOR: feasible={rate_02*100:.1f}% > 20%")
        elif rate_03 > 0.15:
            print(f"  CONSIDER 0.3% FLOOR: feasible={rate_03*100:.1f}% > 15%")
        
        if rate_00 < 0.50:
            print(f"  KILL ENTIRE STRATEGY: Even 0% edge only feasible {rate_00*100:.1f}% of time")
            print(f"  Market is too one-sided for pair capture.")
        
        print()


class StrictArbEngine:
    """
    Strict arbitrage mode - only enter if completion is immediately feasible.
    No rescue, no waiting - pure instant completeable full-set capture.
    """
    
    def __init__(
        self,
        edge_floor: float = 0.005,
        clip_size: float = 5.0,
        output_dir: str = "pm_results_v4",
    ):
        self.edge_floor = edge_floor
        self.clip_size = clip_size
        self.output_dir = Path(output_dir)
        self.output_dir.mkdir(exist_ok=True)
        
        self.fetcher = MarketFetcher()
        
        # Stats
        self.ticks_seen = 0
        self.entry_attempts = 0
        self.entry_skips_not_feasible = 0
        self.first_leg_fills = 0
        self.completions = 0
        self.edges_locked: List[float] = []
        
        # Current state
        self.has_first_leg = False
        self.first_leg_side = None
        self.first_leg_price = 0
        
        # Log
        ts_str = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
        self.log_path = self.output_dir / f"strict_arb_{ts_str}.jsonl"
        self.log_file = None
    
    def _log(self, event: str, data: dict):
        if self.log_file:
            record = {"ts": time.time(), "event": event, **data}
            self.log_file.write(json.dumps(record) + "\n")
            self.log_file.flush()
    
    def _is_feasible(self, tick: OrderBookTick, side: str) -> Tuple[bool, float, float]:
        """
        Check if completion is feasible at edge_floor if we take first leg now.
        Returns (feasible, first_price, cap).
        """
        if side == "up":
            # Take Up at ask_up, complete Down at <= cap
            first_price = tick.ask_up
            cap = 1.0 - self.edge_floor - first_price
            feasible = tick.ask_down <= cap
        else:
            # Take Down at ask_down, complete Up at <= cap
            first_price = tick.ask_down
            cap = 1.0 - self.edge_floor - first_price
            feasible = tick.ask_up <= cap
        
        return feasible, first_price, cap
    
    def _process_tick(self, tick: OrderBookTick, window: Window15Min):
        """Process tick in strict arb mode."""
        # Skip bad ticks
        if tick.ask_up <= 0.01 or tick.ask_down <= 0.01:
            return
        if tick.bid_up <= 0 or tick.bid_down <= 0:
            return
        if tick.ask_up <= tick.bid_up or tick.ask_down <= tick.bid_down:
            return
        
        self.ticks_seen += 1
        
        if self.has_first_leg:
            # Try to complete
            if self.first_leg_side == "up":
                cap = 1.0 - self.edge_floor - self.first_leg_price
                if tick.ask_down <= cap:
                    # Complete!
                    edge = 1.0 - self.first_leg_price - tick.ask_down
                    self.completions += 1
                    self.edges_locked.append(edge)
                    self._log("COMPLETION", {
                        "first_side": "up",
                        "first_price": self.first_leg_price,
                        "comp_price": tick.ask_down,
                        "edge": edge,
                    })
                    logger.info(f"STRICT ARB COMPLETE: edge={edge*100:.2f}c")
                    self.has_first_leg = False
            else:
                cap = 1.0 - self.edge_floor - self.first_leg_price
                if tick.ask_up <= cap:
                    edge = 1.0 - self.first_leg_price - tick.ask_up
                    self.completions += 1
                    self.edges_locked.append(edge)
                    self._log("COMPLETION", {
                        "first_side": "down",
                        "first_price": self.first_leg_price,
                        "comp_price": tick.ask_up,
                        "edge": edge,
                    })
                    logger.info(f"STRICT ARB COMPLETE: edge={edge*100:.2f}c")
                    self.has_first_leg = False
        else:
            # Look for entry
            # Check both sides for feasibility
            feas_up, p_up, cap_up = self._is_feasible(tick, "up")
            feas_down, p_down, cap_down = self._is_feasible(tick, "down")
            
            self.entry_attempts += 1
            
            if feas_up and feas_down:
                # Both feasible - take cheaper first leg
                if p_up <= p_down:
                    self.first_leg_side = "up"
                    self.first_leg_price = p_up
                else:
                    self.first_leg_side = "down"
                    self.first_leg_price = p_down
                
                self.has_first_leg = True
                self.first_leg_fills += 1
                self._log("FIRST_LEG", {
                    "side": self.first_leg_side,
                    "price": self.first_leg_price,
                    "cap": 1.0 - self.edge_floor - self.first_leg_price,
                })
                logger.info(f"STRICT ARB ENTRY: {self.first_leg_side} @ {self.first_leg_price:.4f}")
            
            elif feas_up:
                self.first_leg_side = "up"
                self.first_leg_price = p_up
                self.has_first_leg = True
                self.first_leg_fills += 1
                self._log("FIRST_LEG", {"side": "up", "price": p_up})
                logger.info(f"STRICT ARB ENTRY: up @ {p_up:.4f}")
            
            elif feas_down:
                self.first_leg_side = "down"
                self.first_leg_price = p_down
                self.has_first_leg = True
                self.first_leg_fills += 1
                self._log("FIRST_LEG", {"side": "down", "price": p_down})
                logger.info(f"STRICT ARB ENTRY: down @ {p_down:.4f}")
            
            else:
                self.entry_skips_not_feasible += 1
    
    def run(self, target_fills: int = 20, min_fills: int = 5, max_minutes: float = 60):
        """Run strict arb mode."""
        print("\n" + "=" * 70)
        print("STRICT ARB MODE - No Rescue, Instant Complete Only")
        print("=" * 70)
        print(f"Edge floor: {self.edge_floor*100:.1f}%")
        print(f"Target fills: {target_fills}, Min: {min_fills}")
        print(f"Output: {self.log_path}")
        print("=" * 70)
        
        start = time.time()
        deadline = start + max_minutes * 60
        current_window = None
        window = None
        
        self.log_file = open(self.log_path, "w", encoding="utf-8")
        
        try:
            while time.time() < deadline and self.first_leg_fills < target_fills:
                slug = get_current_window_slug()
                if slug != current_window:
                    current_window = slug
                    window = self.fetcher.fetch_market_by_slug(slug)
                    if window:
                        logger.info(f"Window: {slug}")
                        self._log("WINDOW", {"slug": slug})
                        # Reset first leg on new window
                        self.has_first_leg = False
                
                if window:
                    tick = self.fetcher.fetch_tick(window)
                    if tick:
                        self._process_tick(tick, window)
                
                time.sleep(0.3)
                
                # Progress
                if self.ticks_seen % 100 == 0:
                    skip_rate = self.entry_skips_not_feasible / max(1, self.entry_attempts) * 100
                    print(f"[{(time.time()-start)/60:.1f}m] Ticks: {self.ticks_seen} | "
                          f"Entries: {self.first_leg_fills} | Completions: {self.completions} | "
                          f"Skip rate: {skip_rate:.1f}%")
        
        except KeyboardInterrupt:
            logger.info("Interrupted")
        finally:
            self.log_file.close()
        
        self._print_summary()
    
    def _print_summary(self):
        """Print summary."""
        print("\n" + "=" * 70)
        print("STRICT ARB SUMMARY")
        print("=" * 70)
        print(f"Ticks: {self.ticks_seen}")
        print(f"Entry attempts: {self.entry_attempts}")
        print(f"Skipped (not feasible): {self.entry_skips_not_feasible} "
              f"({self.entry_skips_not_feasible/max(1,self.entry_attempts)*100:.1f}%)")
        print(f"First leg fills: {self.first_leg_fills}")
        print(f"Completions: {self.completions}")
        
        if self.first_leg_fills > 0:
            comp_rate = self.completions / self.first_leg_fills * 100
            print(f"Completion rate: {comp_rate:.1f}%")
        
        if self.edges_locked:
            median_edge = statistics.median(self.edges_locked) * 100
            p10_edge = sorted(self.edges_locked)[len(self.edges_locked)//10] * 100 if len(self.edges_locked) >= 10 else min(self.edges_locked) * 100
            print(f"Edge locked: median={median_edge:.2f}c, p10={p10_edge:.2f}c")
        
        # Decision
        print("\n" + "-" * 70)
        if self.first_leg_fills == 0:
            print("KILL: No entries possible at this edge floor")
        elif self.completions / max(1, self.first_leg_fills) < 0.5:
            print(f"KILL: Completion rate {self.completions/self.first_leg_fills*100:.1f}% < 50%")
        elif self.edges_locked and statistics.median(self.edges_locked) < 0.002:
            print(f"MARGINAL: Median edge {statistics.median(self.edges_locked)*100:.2f}c < 0.2c")
        else:
            print("POTENTIALLY VIABLE: High completion rate with meaningful edge")


class MakerWaitEngine:
    """
    Maker wait mode - after first leg fill, post maker order and wait for fill.
    """
    
    def __init__(
        self,
        edge_floor: float = 0.005,
        wait_ms: float = 2000,  # Max wait time for maker fill
        clip_size: float = 5.0,
        output_dir: str = "pm_results_v4",
    ):
        self.edge_floor = edge_floor
        self.wait_ms = wait_ms
        self.clip_size = clip_size
        self.output_dir = Path(output_dir)
        self.output_dir.mkdir(exist_ok=True)
        
        self.fetcher = MarketFetcher()
        
        # Stats
        self.ticks_seen = 0
        self.first_leg_fills = 0
        self.maker_waits = 0
        self.maker_fills = 0
        self.maker_timeouts = 0
        self.edges_locked: List[float] = []
        
        # Current state
        self.has_first_leg = False
        self.first_leg_side = None
        self.first_leg_price = 0
        self.wait_start_ts = 0
        self.maker_bid_price = 0
        
        # Log
        ts_str = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
        self.log_path = self.output_dir / f"maker_wait_{ts_str}.jsonl"
        self.log_file = None
    
    def _log(self, event: str, data: dict):
        if self.log_file:
            record = {"ts": time.time(), "event": event, **data}
            self.log_file.write(json.dumps(record) + "\n")
            self.log_file.flush()
    
    def _process_tick(self, tick: OrderBookTick, window: Window15Min):
        """Process tick in maker wait mode."""
        # Skip bad ticks
        if tick.ask_up <= 0.01 or tick.ask_down <= 0.01:
            return
        if tick.bid_up <= 0 or tick.bid_down <= 0:
            return
        if tick.ask_up <= tick.bid_up or tick.ask_down <= tick.bid_down:
            return
        
        self.ticks_seen += 1
        now = time.time()
        
        if self.has_first_leg:
            # Waiting for maker fill
            elapsed_ms = (now - self.wait_start_ts) * 1000
            
            # Check for fill (ask crosses our bid)
            if self.first_leg_side == "up":
                other_ask = tick.ask_down
            else:
                other_ask = tick.ask_up
            
            if other_ask <= self.maker_bid_price:
                # Maker fill!
                edge = 1.0 - self.first_leg_price - self.maker_bid_price
                self.maker_fills += 1
                self.edges_locked.append(edge)
                self._log("MAKER_FILL", {
                    "first_side": self.first_leg_side,
                    "first_price": self.first_leg_price,
                    "maker_bid": self.maker_bid_price,
                    "fill_price": other_ask,
                    "edge": edge,
                    "wait_ms": elapsed_ms,
                })
                logger.info(f"MAKER FILL: edge={edge*100:.2f}c, wait={elapsed_ms:.0f}ms")
                self.has_first_leg = False
            
            elif elapsed_ms > self.wait_ms:
                # Timeout
                self.maker_timeouts += 1
                self._log("MAKER_TIMEOUT", {
                    "first_side": self.first_leg_side,
                    "first_price": self.first_leg_price,
                    "maker_bid": self.maker_bid_price,
                    "wait_ms": elapsed_ms,
                    "final_ask": other_ask,
                })
                logger.warning(f"MAKER TIMEOUT: waited {elapsed_ms:.0f}ms")
                self.has_first_leg = False
        
        else:
            # Look for entry - use cheaper side as first leg
            if tick.bid_up <= tick.bid_down:
                first_side = "up"
                first_price = tick.ask_up  # Taker into first leg
            else:
                first_side = "down"
                first_price = tick.ask_down
            
            # Compute maker bid for completion
            cap = 1.0 - self.edge_floor - first_price
            
            # Only enter if maker bid is at least somewhat competitive
            if first_side == "up":
                other_bid = tick.bid_down
            else:
                other_bid = tick.bid_up
            
            # Our maker bid needs to be at or below cap
            maker_bid = min(cap, other_bid + 0.01)  # Slightly improve current bid
            
            if maker_bid > 0.05:  # Sanity check
                self.first_leg_side = first_side
                self.first_leg_price = first_price
                self.maker_bid_price = maker_bid
                self.has_first_leg = True
                self.first_leg_fills += 1
                self.maker_waits += 1
                self.wait_start_ts = now
                
                self._log("FIRST_LEG_MAKER_POST", {
                    "side": first_side,
                    "first_price": first_price,
                    "maker_bid": maker_bid,
                    "cap": cap,
                })
                logger.info(f"MAKER WAIT: {first_side} @ {first_price:.4f}, bid={maker_bid:.4f}")
    
    def run(self, target_fills: int = 20, min_fills: int = 5, max_minutes: float = 60):
        """Run maker wait mode."""
        print("\n" + "=" * 70)
        print("MAKER WAIT MODE - Post and Wait for Completion")
        print("=" * 70)
        print(f"Edge floor: {self.edge_floor*100:.1f}%")
        print(f"Wait timeout: {self.wait_ms}ms")
        print(f"Target fills: {target_fills}")
        print(f"Output: {self.log_path}")
        print("=" * 70)
        
        start = time.time()
        deadline = start + max_minutes * 60
        current_window = None
        window = None
        
        self.log_file = open(self.log_path, "w", encoding="utf-8")
        
        try:
            while time.time() < deadline and self.first_leg_fills < target_fills:
                slug = get_current_window_slug()
                if slug != current_window:
                    current_window = slug
                    window = self.fetcher.fetch_market_by_slug(slug)
                    if window:
                        logger.info(f"Window: {slug}")
                        self._log("WINDOW", {"slug": slug})
                        self.has_first_leg = False
                
                if window:
                    tick = self.fetcher.fetch_tick(window)
                    if tick:
                        self._process_tick(tick, window)
                
                time.sleep(0.2)  # Faster polling for maker wait
                
                if self.ticks_seen % 100 == 0:
                    fill_rate = self.maker_fills / max(1, self.maker_waits) * 100
                    print(f"[{(time.time()-start)/60:.1f}m] First legs: {self.first_leg_fills} | "
                          f"Maker fills: {self.maker_fills} | Timeouts: {self.maker_timeouts} | "
                          f"Fill rate: {fill_rate:.1f}%")
        
        except KeyboardInterrupt:
            logger.info("Interrupted")
        finally:
            self.log_file.close()
        
        self._print_summary()
    
    def _print_summary(self):
        """Print summary."""
        print("\n" + "=" * 70)
        print("MAKER WAIT SUMMARY")
        print("=" * 70)
        print(f"Ticks: {self.ticks_seen}")
        print(f"First leg fills: {self.first_leg_fills}")
        print(f"Maker waits: {self.maker_waits}")
        print(f"Maker fills: {self.maker_fills}")
        print(f"Maker timeouts: {self.maker_timeouts}")
        
        if self.maker_waits > 0:
            fill_rate = self.maker_fills / self.maker_waits * 100
            print(f"Maker fill rate: {fill_rate:.1f}%")
        
        if self.edges_locked:
            median_edge = statistics.median(self.edges_locked) * 100
            print(f"Edge locked: median={median_edge:.2f}c")
        
        print()


def main():
    import argparse
    
    logging.basicConfig(
        level=logging.INFO, 
        format="%(asctime)s | %(levelname)s | %(message)s", 
        datefmt="%H:%M:%S"
    )
    
    parser = argparse.ArgumentParser(description="Feasibility Analyzer")
    parser.add_argument("--shadow-sample", action="store_true", help="Shadow sample mode (no trading)")
    parser.add_argument("--strict-arb", action="store_true", help="Strict arb mode (no rescue)")
    parser.add_argument("--maker-wait-ms", type=int, default=0, help="Maker wait mode with timeout (ms)")
    parser.add_argument("--max-minutes", type=float, default=30, help="Max run time in minutes")
    parser.add_argument("--target-fills", type=int, default=20, help="Target first-leg fills")
    parser.add_argument("--min-fills", type=int, default=5, help="Min fills for decision")
    parser.add_argument("--edge-floor", type=float, default=0.005, help="Edge floor (default 0.5%)")
    
    args = parser.parse_args()
    
    if args.shadow_sample:
        analyzer = FeasibilityAnalyzer()
        analyzer.run_shadow_sample(max_minutes=args.max_minutes)
    
    elif args.strict_arb:
        engine = StrictArbEngine(edge_floor=args.edge_floor)
        engine.run(
            target_fills=args.target_fills,
            min_fills=args.min_fills,
            max_minutes=args.max_minutes
        )
    
    elif args.maker_wait_ms > 0:
        engine = MakerWaitEngine(
            edge_floor=args.edge_floor,
            wait_ms=args.maker_wait_ms
        )
        engine.run(
            target_fills=args.target_fills,
            min_fills=args.min_fills,
            max_minutes=args.max_minutes
        )
    
    else:
        print("Usage:")
        print("  Shadow sample: python -m pm_15m_arb.feasibility --shadow-sample --max-minutes 30")
        print("  Strict arb:    python -m pm_15m_arb.feasibility --strict-arb --target-fills 20")
        print("  Maker wait:    python -m pm_15m_arb.feasibility --maker-wait-ms 2000 --target-fills 20")


if __name__ == "__main__":
    main()

