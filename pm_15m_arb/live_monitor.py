"""
Live Monitor - Continuous BTC 15-min Market Scanner

Monitors markets in real-time and logs all observations.
Used to determine if arbitrage opportunities ever appear.
"""

import logging
import time
import json
from datetime import datetime, timezone
from pathlib import Path

from .direct_scanner import DirectScanner, BTCMarketData
from .config import ArbConfig, load_config

logger = logging.getLogger(__name__)


class LiveMonitor:
    """
    Continuously monitors BTC 15-min markets for arb opportunities.
    
    Logs all observations to JSONL for later analysis.
    """
    
    def __init__(self, config: ArbConfig = None):
        self.config = config or load_config()
        self.scanner = DirectScanner(config)
        
        # Stats
        self.scans = 0
        self.arb_opportunities = 0
        self.min_sum_seen = 999
        self.max_edge_seen = -999
        
        # History
        self.history = []
        
        # Log file
        self.log_file = Path("btc_15m_monitor.jsonl")
    
    def _log_observation(self, data: BTCMarketData):
        """Log a market observation."""
        obs = {
            "ts": datetime.now(timezone.utc).isoformat(),
            "event": data.event_slug,
            "ask_up": data.ask_up,
            "ask_down": data.ask_down,
            "sum": data.sum_asks,
            "edge": data.edge,
            "has_arb": data.has_arb,
            "size_up": data.size_up,
            "size_down": data.size_down,
            "seconds_remaining": data.seconds_remaining,
        }
        
        with open(self.log_file, "a") as f:
            f.write(json.dumps(obs) + "\n")
        
        self.history.append(obs)
        
        # Update stats
        if data.sum_asks < self.min_sum_seen:
            self.min_sum_seen = data.sum_asks
        if data.edge > self.max_edge_seen:
            self.max_edge_seen = data.edge
        if data.has_arb:
            self.arb_opportunities += 1
    
    def scan_once(self) -> list:
        """Run a single scan."""
        self.scans += 1
        results = self.scanner.scan_all()
        
        for data in results:
            self._log_observation(data)
        
        return results
    
    def run(self, duration_seconds: int = 300, interval_seconds: float = 2.0):
        """
        Run continuous monitoring.
        
        Args:
            duration_seconds: How long to run (default 5 minutes)
            interval_seconds: Time between scans
        """
        print("\n" + "="*70)
        print("BTC 15-min Market Live Monitor")
        print("="*70)
        print(f"Duration: {duration_seconds}s | Interval: {interval_seconds}s")
        print(f"Logging to: {self.log_file}")
        print("="*70)
        print("\nPress Ctrl+C to stop early.\n")
        
        start_time = time.time()
        
        try:
            while time.time() - start_time < duration_seconds:
                results = self.scan_once()
                
                # Print summary
                ts = datetime.now().strftime("%H:%M:%S")
                
                if results:
                    best = results[0]  # Sorted by edge
                    arb = "ARB!" if best.has_arb else "no"
                    print(f"[{ts}] Scan #{self.scans}: {len(results)} markets | "
                          f"Best sum={best.sum_asks:.4f} edge={best.edge:.4f} | "
                          f"Arb: {arb}")
                else:
                    print(f"[{ts}] Scan #{self.scans}: No markets found")
                
                time.sleep(interval_seconds)
        
        except KeyboardInterrupt:
            print("\n\nStopped by user.")
        
        self._print_summary()
    
    def _print_summary(self):
        """Print monitoring summary."""
        print("\n" + "="*70)
        print("MONITORING SUMMARY")
        print("="*70)
        
        print(f"\nTotal scans: {self.scans}")
        print(f"Arb opportunities seen: {self.arb_opportunities}")
        print(f"Min sum observed: {self.min_sum_seen:.4f}")
        print(f"Max edge observed: {self.max_edge_seen:.4f} ({self.max_edge_seen*100:.2f}%)")
        
        # Analyze history
        if self.history:
            sums = [h["sum"] for h in self.history]
            avg_sum = sum(sums) / len(sums)
            
            print(f"\nAverage sum of asks: {avg_sum:.4f}")
            print(f"Observations: {len(self.history)}")
            
            # Distribution
            below_99 = sum(1 for s in sums if s < 0.99)
            below_100 = sum(1 for s in sums if s < 1.00)
            at_100_101 = sum(1 for s in sums if 1.00 <= s < 1.01)
            above_101 = sum(1 for s in sums if s >= 1.01)
            
            print(f"\nSum distribution:")
            print(f"  < $0.99 (ARB!):  {below_99} ({below_99/len(sums)*100:.1f}%)")
            print(f"  < $1.00:         {below_100} ({below_100/len(sums)*100:.1f}%)")
            print(f"  $1.00-$1.01:     {at_100_101} ({at_100_101/len(sums)*100:.1f}%)")
            print(f"  > $1.01:         {above_101} ({above_101/len(sums)*100:.1f}%)")
        
        print(f"\nLog file: {self.log_file}")
        
        # Verdict
        print("\n" + "-"*70)
        if self.arb_opportunities > 0:
            print("VERDICT: Arbitrage opportunities DO exist in this market!")
            print(f"  Seen {self.arb_opportunities} times during monitoring")
        elif self.max_edge_seen > -0.01:
            print("VERDICT: Near-arb conditions observed (edge close to 0)")
            print("  May need faster execution or tighter timing")
        else:
            print("VERDICT: No arbitrage opportunities observed")
            print("  Sum of asks consistently > $1.00")
            print("  Market makers are efficient - no free lunch")


def run_monitor(duration: int = 120):
    """Run the live monitor."""
    monitor = LiveMonitor()
    monitor.run(duration_seconds=duration, interval_seconds=2.0)


if __name__ == "__main__":
    logging.basicConfig(
        level=logging.WARNING,  # Reduce noise
        format="%(asctime)s | %(message)s",
        datefmt="%H:%M:%S"
    )
    
    print("Starting 2-minute monitor...")
    run_monitor(duration=120)

