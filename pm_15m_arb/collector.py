"""
Data Collector - Phase 2

Collects orderbook snapshots across multiple 15-minute windows.
Saves to JSONL for later analysis.

Goal: Collect 200-500 windows of data before judging anything.
"""

import logging
import time
import json
import gzip
from pathlib import Path
from datetime import datetime, timezone
from typing import Optional, List, Dict
from dataclasses import dataclass, field

from .market_v2 import (
    MarketFetcher, 
    Window15Min, 
    OrderBookTick,
    get_current_window_slug,
    get_next_window_slug,
)

logger = logging.getLogger(__name__)


@dataclass
class WindowStats:
    """Statistics for a single 15-minute window."""
    slug: str
    start_ts: int
    end_ts: int
    
    ticks_collected: int = 0
    
    # Price extremes
    min_ask_up: float = 999.0
    max_ask_up: float = 0.0
    min_ask_down: float = 999.0
    max_ask_down: float = 0.0
    
    min_ask_sum: float = 999.0
    max_ask_sum: float = 0.0
    
    # Bid extremes
    min_bid_sum: float = 999.0
    max_bid_sum: float = 0.0
    
    # For averaging
    sum_ask_up: float = 0.0
    sum_ask_down: float = 0.0
    sum_bid_up: float = 0.0
    sum_bid_down: float = 0.0
    
    def update(self, tick: OrderBookTick):
        """Update stats with a new tick."""
        self.ticks_collected += 1
        
        # Price extremes
        if tick.ask_up > 0:
            self.min_ask_up = min(self.min_ask_up, tick.ask_up)
            self.max_ask_up = max(self.max_ask_up, tick.ask_up)
            self.sum_ask_up += tick.ask_up
        
        if tick.ask_down > 0:
            self.min_ask_down = min(self.min_ask_down, tick.ask_down)
            self.max_ask_down = max(self.max_ask_down, tick.ask_down)
            self.sum_ask_down += tick.ask_down
        
        if tick.ask_sum > 0:
            self.min_ask_sum = min(self.min_ask_sum, tick.ask_sum)
            self.max_ask_sum = max(self.max_ask_sum, tick.ask_sum)
        
        if tick.bid_sum > 0:
            self.min_bid_sum = min(self.min_bid_sum, tick.bid_sum)
            self.max_bid_sum = max(self.max_bid_sum, tick.bid_sum)
        
        if tick.bid_up > 0:
            self.sum_bid_up += tick.bid_up
        if tick.bid_down > 0:
            self.sum_bid_down += tick.bid_down
    
    @property
    def avg_ask_up(self) -> float:
        return self.sum_ask_up / self.ticks_collected if self.ticks_collected > 0 else 0
    
    @property
    def avg_ask_down(self) -> float:
        return self.sum_ask_down / self.ticks_collected if self.ticks_collected > 0 else 0
    
    @property
    def avg_ask_sum(self) -> float:
        return self.avg_ask_up + self.avg_ask_down
    
    def to_dict(self) -> dict:
        return {
            "slug": self.slug,
            "start_ts": self.start_ts,
            "end_ts": self.end_ts,
            "ticks_collected": self.ticks_collected,
            "min_ask_up": self.min_ask_up if self.min_ask_up < 999 else 0,
            "max_ask_up": self.max_ask_up,
            "min_ask_down": self.min_ask_down if self.min_ask_down < 999 else 0,
            "max_ask_down": self.max_ask_down,
            "min_ask_sum": self.min_ask_sum if self.min_ask_sum < 999 else 0,
            "max_ask_sum": self.max_ask_sum,
            "min_bid_sum": self.min_bid_sum if self.min_bid_sum < 999 else 0,
            "max_bid_sum": self.max_bid_sum,
            "avg_ask_up": self.avg_ask_up,
            "avg_ask_down": self.avg_ask_down,
            "avg_ask_sum": self.avg_ask_sum,
        }


class DataCollector:
    """
    Collects orderbook data across multiple windows.
    
    Features:
    - Polls at configurable interval (default 500ms)
    - Automatically tracks window boundaries
    - Saves all ticks to JSONL
    - Computes per-window statistics
    """
    
    def __init__(
        self,
        output_dir: str = "pm_data",
        poll_interval_ms: int = 500,
    ):
        self.output_dir = Path(output_dir)
        self.output_dir.mkdir(exist_ok=True)
        
        self.poll_interval = poll_interval_ms / 1000.0
        
        self.fetcher = MarketFetcher()
        
        # Current window tracking
        self.current_window: Optional[Window15Min] = None
        self.current_stats: Optional[WindowStats] = None
        
        # Session stats
        self.windows_collected: List[WindowStats] = []
        self.total_ticks = 0
        
        # File handles
        self.tick_file = None
        self.session_start = datetime.now(timezone.utc)
        self._open_tick_file()
    
    def _open_tick_file(self):
        """Open the tick log file."""
        ts = self.session_start.strftime("%Y%m%d_%H%M%S")
        self.tick_path = self.output_dir / f"ticks_{ts}.jsonl"
        self.tick_file = open(self.tick_path, "a")
        logger.info(f"Logging ticks to: {self.tick_path}")
    
    def _log_tick(self, tick: OrderBookTick):
        """Write a tick to the log file."""
        if self.tick_file:
            self.tick_file.write(json.dumps(tick.to_dict()) + "\n")
            self.tick_file.flush()
    
    def _switch_window(self, new_slug: str):
        """Handle window transition."""
        # Save stats for completed window
        if self.current_stats and self.current_stats.ticks_collected > 0:
            self.windows_collected.append(self.current_stats)
            logger.info(f"Window {self.current_stats.slug} completed: "
                       f"{self.current_stats.ticks_collected} ticks, "
                       f"ask_sum range [{self.current_stats.min_ask_sum:.4f}, {self.current_stats.max_ask_sum:.4f}]")
        
        # Fetch new window metadata
        self.current_window = self.fetcher.fetch_market_by_slug(new_slug)
        
        if self.current_window:
            self.current_stats = WindowStats(
                slug=self.current_window.slug,
                start_ts=self.current_window.start_ts,
                end_ts=self.current_window.end_ts,
            )
            logger.info(f"Switched to window: {new_slug}")
        else:
            self.current_stats = None
            logger.warning(f"Could not fetch window: {new_slug}")
    
    def collect_one_tick(self) -> Optional[OrderBookTick]:
        """Collect a single tick."""
        current_slug = get_current_window_slug()
        
        # Check if we need to switch windows
        if self.current_window is None or self.current_window.slug != current_slug:
            self._switch_window(current_slug)
        
        if not self.current_window:
            return None
        
        # Fetch tick
        tick = self.fetcher.fetch_tick(self.current_window)
        
        if tick and tick.ask_up > 0 and tick.ask_down > 0:
            self._log_tick(tick)
            self.total_ticks += 1
            
            if self.current_stats:
                self.current_stats.update(tick)
            
            return tick
        
        return None
    
    def run(self, duration_minutes: int = 60, windows_target: int = 0):
        """
        Run the collector.
        
        Args:
            duration_minutes: How long to run (default 60 min = 4 windows)
            windows_target: Stop after this many windows (0 = use duration)
        """
        print("\n" + "="*70)
        print("BTC 15-min Data Collector")
        print("="*70)
        print(f"Poll interval: {self.poll_interval*1000:.0f}ms")
        print(f"Output: {self.tick_path}")
        if windows_target > 0:
            print(f"Target: {windows_target} windows")
        else:
            print(f"Duration: {duration_minutes} minutes")
        print("="*70)
        print("\nPress Ctrl+C to stop.\n")
        
        start_time = time.time()
        duration_sec = duration_minutes * 60
        
        last_print = 0
        
        try:
            while True:
                # Check stop conditions
                elapsed = time.time() - start_time
                
                if windows_target > 0:
                    if len(self.windows_collected) >= windows_target:
                        print(f"\nReached target: {windows_target} windows")
                        break
                elif elapsed >= duration_sec:
                    print(f"\nReached duration: {duration_minutes} minutes")
                    break
                
                # Collect tick
                tick = self.collect_one_tick()
                
                # Print status every 10 seconds
                if time.time() - last_print >= 10:
                    last_print = time.time()
                    
                    if tick:
                        remaining = tick.seconds_remaining
                        print(f"[{datetime.now().strftime('%H:%M:%S')}] "
                              f"Window {len(self.windows_collected)+1} | "
                              f"{remaining:.0f}s left | "
                              f"Up={tick.ask_up:.2f} Down={tick.ask_down:.2f} "
                              f"Sum={tick.ask_sum:.4f} | "
                              f"Ticks: {self.total_ticks}")
                    else:
                        print(f"[{datetime.now().strftime('%H:%M:%S')}] "
                              f"Waiting for market data...")
                
                time.sleep(self.poll_interval)
        
        except KeyboardInterrupt:
            print("\n\nStopped by user.")
        
        finally:
            # Close file
            if self.tick_file:
                self.tick_file.close()
            
            self._print_summary()
            self._save_summary()
    
    def _print_summary(self):
        """Print collection summary."""
        print("\n" + "="*70)
        print("COLLECTION SUMMARY")
        print("="*70)
        
        print(f"\nTotal ticks: {self.total_ticks}")
        print(f"Windows completed: {len(self.windows_collected)}")
        print(f"Tick file: {self.tick_path}")
        
        if not self.windows_collected:
            print("\nNo complete windows collected.")
            return
        
        # Aggregate stats
        all_min_sums = [w.min_ask_sum for w in self.windows_collected if w.min_ask_sum < 999]
        all_max_sums = [w.max_ask_sum for w in self.windows_collected if w.max_ask_sum > 0]
        
        if all_min_sums:
            global_min = min(all_min_sums)
            global_max = max(all_max_sums) if all_max_sums else 0
            
            print(f"\nAsk Sum Range (across all windows):")
            print(f"  Global Min: {global_min:.4f}")
            print(f"  Global Max: {global_max:.4f}")
            
            # Check for instant arb opportunities
            below_1 = sum(1 for s in all_min_sums if s < 1.0)
            print(f"\nWindows with ask_sum < 1.0 at any point: {below_1} / {len(self.windows_collected)}")
        
        # Per-window breakdown
        print(f"\nPer-Window Stats:")
        print("-"*70)
        
        for w in self.windows_collected[-10:]:  # Last 10
            print(f"  {w.slug}: ticks={w.ticks_collected}, "
                  f"ask_sum=[{w.min_ask_sum:.4f}, {w.max_ask_sum:.4f}], "
                  f"avg={w.avg_ask_sum:.4f}")
    
    def _save_summary(self):
        """Save summary to file."""
        summary_path = self.output_dir / f"summary_{self.session_start.strftime('%Y%m%d_%H%M%S')}.json"
        
        summary = {
            "session_start": self.session_start.isoformat(),
            "session_end": datetime.now(timezone.utc).isoformat(),
            "total_ticks": self.total_ticks,
            "windows_completed": len(self.windows_collected),
            "tick_file": str(self.tick_path),
            "windows": [w.to_dict() for w in self.windows_collected],
        }
        
        with open(summary_path, "w") as f:
            json.dump(summary, f, indent=2)
        
        print(f"\nSummary saved: {summary_path}")


def run_collector(duration_minutes: int = 30, windows: int = 0):
    """Run the data collector."""
    collector = DataCollector(poll_interval_ms=500)
    collector.run(duration_minutes=duration_minutes, windows_target=windows)


if __name__ == "__main__":
    import argparse
    
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s | %(levelname)s | %(message)s",
        datefmt="%H:%M:%S"
    )
    
    parser = argparse.ArgumentParser(description="Collect BTC 15-min orderbook data")
    parser.add_argument("--duration", type=int, default=30, help="Duration in minutes (default 30)")
    parser.add_argument("--windows", type=int, default=0, help="Stop after N windows (0 = use duration)")
    parser.add_argument("--poll-ms", type=int, default=500, help="Poll interval in ms (default 500)")
    
    args = parser.parse_args()
    
    collector = DataCollector(poll_interval_ms=args.poll_ms)
    collector.run(duration_minutes=args.duration, windows_target=args.windows)

