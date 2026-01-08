"""
HYBRID STRATEGY: The only profitable way to trade 90-99c
Based on 50 days of empirical data analysis.

Key insight: Directional 90c+ entries are NEVER profitable.
The ONLY edge is capturing full-sets when combined cost < 100c.
"""
from dataclasses import dataclass, field
from typing import List, Optional, Literal
from enum import Enum

from .stream import QuoteTick, WindowData
from .config import StrategyConfig


class HybridState(Enum):
    """Strategy state machine."""
    SCANNING = "scanning"           # Looking for opportunities
    ACCUMULATING = "accumulating"   # Building full-set position
    HOLDING = "holding"             # Holding to settlement


@dataclass
class Position:
    """Current position in a side."""
    side: str  # "UP" or "DOWN"
    size: float = 0.0
    avg_price: float = 0.0
    entry_time: float = 0.0


@dataclass
class HybridTrade:
    """A completed trade record."""
    window_id: str
    strategy: Literal["FULLSET", "SPIKE"]
    
    # Entry details
    up_entry_price: Optional[float] = None
    up_entry_time: Optional[float] = None
    down_entry_price: Optional[float] = None
    down_entry_time: Optional[float] = None
    
    # Combined metrics
    combined_cost: float = 0.0
    theoretical_edge: float = 0.0  # 100 - combined_cost
    
    # Exit
    exit_reason: str = ""
    pnl: float = 0.0


@dataclass 
class HybridConfig:
    """Configuration for hybrid strategy."""
    
    # Full-set parameters
    max_combined_cost: int = 99  # Only take full-set if combined < this
    min_edge_cents: int = 1      # Minimum edge to take trade
    
    # Spike parameters (for opportunistic additions)
    spike_enabled: bool = False   # Disabled by default - not profitable!
    min_spike_price: int = 93     # Only consider spikes above this
    max_opposite_for_spike: int = 5  # Only spike if can complete full-set
    
    # Execution
    size_per_trade: float = 10.0  # Dollars per leg
    
    # Timing
    min_time_remaining: float = 60.0  # Don't enter with < 1 min left


class HybridStrategy:
    """
    The hybrid strategy that ACTUALLY works:
    
    1. FULL-SET ACCUMULATION (Primary):
       - Every tick, check if UP_ask + DOWN_ask < max_combined_cost
       - If yes, buy BOTH sides immediately
       - Hold to settlement for guaranteed profit
    
    2. SPIKE OPPORTUNISTIC (Secondary, disabled by default):
       - If one side spikes to 93c+ AND opposite is < 5c
       - This is effectively a full-set with one leg already "won"
       - Only enabled if spike_enabled=True
    
    The math is simple:
    - Full-set at 98c cost = 2c guaranteed profit per pair
    - With 100 windows per day = 200c = $2/day guaranteed
    - Scale with size for more
    """
    
    def __init__(self, config: HybridConfig):
        self.config = config
    
    def run_window(self, window: WindowData) -> List[HybridTrade]:
        """Process a single window and return any trades."""
        trades = []
        
        if not window.ticks:
            return trades
        
        # Track if we've taken a position this window
        position_taken = False
        current_trade: Optional[HybridTrade] = None
        
        for tick in window.ticks:
            if position_taken:
                continue
            
            time_remaining = 900 - tick.elapsed_secs
            if time_remaining < self.config.min_time_remaining:
                continue
            
            # Check for full-set opportunity
            combined = tick.up_ask + tick.down_ask
            edge = 100 - combined
            
            if combined <= self.config.max_combined_cost and edge >= self.config.min_edge_cents:
                # FULL-SET OPPORTUNITY!
                current_trade = HybridTrade(
                    window_id=window.window_id,
                    strategy="FULLSET",
                    up_entry_price=tick.up_ask,
                    up_entry_time=tick.elapsed_secs,
                    down_entry_price=tick.down_ask,
                    down_entry_time=tick.elapsed_secs,
                    combined_cost=combined,
                    theoretical_edge=edge,
                    exit_reason="SETTLEMENT",
                    pnl=edge / 100.0 * self.config.size_per_trade  # Guaranteed profit
                )
                trades.append(current_trade)
                position_taken = True
                continue
            
            # Check for spike opportunity (if enabled)
            if self.config.spike_enabled:
                # UP spike with LOW down
                if tick.up_ask >= self.config.min_spike_price:
                    if tick.down_ask <= self.config.max_opposite_for_spike:
                        combined = tick.up_ask + tick.down_ask
                        edge = 100 - combined
                        if edge >= self.config.min_edge_cents:
                            current_trade = HybridTrade(
                                window_id=window.window_id,
                                strategy="SPIKE_FULLSET",
                                up_entry_price=tick.up_ask,
                                up_entry_time=tick.elapsed_secs,
                                down_entry_price=tick.down_ask,
                                down_entry_time=tick.elapsed_secs,
                                combined_cost=combined,
                                theoretical_edge=edge,
                                exit_reason="SETTLEMENT",
                                pnl=edge / 100.0 * self.config.size_per_trade
                            )
                            trades.append(current_trade)
                            position_taken = True
                            continue
                
                # DOWN spike with LOW up
                if tick.down_ask >= self.config.min_spike_price:
                    if tick.up_ask <= self.config.max_opposite_for_spike:
                        combined = tick.up_ask + tick.down_ask
                        edge = 100 - combined
                        if edge >= self.config.min_edge_cents:
                            current_trade = HybridTrade(
                                window_id=window.window_id,
                                strategy="SPIKE_FULLSET",
                                up_entry_price=tick.up_ask,
                                up_entry_time=tick.elapsed_secs,
                                down_entry_price=tick.down_ask,
                                down_entry_time=tick.elapsed_secs,
                                combined_cost=combined,
                                theoretical_edge=edge,
                                exit_reason="SETTLEMENT",
                                pnl=edge / 100.0 * self.config.size_per_trade
                            )
                            trades.append(current_trade)
                            position_taken = True
        
        return trades


def run_hybrid_backtest(
    max_combined_cost: int = 99,
    min_edge: int = 1,
    size: float = 10.0
):
    """Run the hybrid strategy on all windows."""
    from .stream import load_all_windows
    from .config import DEFAULT_BUY_DIR, DEFAULT_SELL_DIR
    import os
    import json
    import csv
    
    config = HybridConfig(
        max_combined_cost=max_combined_cost,
        min_edge_cents=min_edge,
        size_per_trade=size
    )
    
    strategy = HybridStrategy(config)
    
    print("="*60)
    print("HYBRID FULL-SET STRATEGY BACKTEST")
    print("="*60)
    print(f"\nConfig:")
    print(f"  Max combined cost: {max_combined_cost}c")
    print(f"  Min edge required: {min_edge}c")
    print(f"  Size per trade: ${size}")
    print()
    
    windows = load_all_windows(DEFAULT_BUY_DIR, DEFAULT_SELL_DIR)
    print(f"Loaded {len(windows)} windows")
    
    all_trades = []
    for window in windows:
        trades = strategy.run_window(window)
        all_trades.extend(trades)
    
    # Compute stats
    print(f"\n{'='*60}")
    print("RESULTS")
    print("="*60)
    
    total_trades = len(all_trades)
    total_pnl = sum(t.pnl for t in all_trades)
    
    if total_trades > 0:
        avg_edge = sum(t.theoretical_edge for t in all_trades) / total_trades
        avg_combined = sum(t.combined_cost for t in all_trades) / total_trades
        
        # Edge distribution
        edge_dist = {}
        for t in all_trades:
            bucket = int(t.theoretical_edge)
            edge_dist[bucket] = edge_dist.get(bucket, 0) + 1
        
        print(f"\nTrades: {total_trades}")
        print(f"Windows with opportunity: {total_trades / len(windows) * 100:.1f}%")
        print(f"Avg combined cost: {avg_combined:.1f}c")
        print(f"Avg edge per trade: {avg_edge:.2f}c")
        print(f"Total PnL: ${total_pnl:.2f}")
        print(f"PnL per window: ${total_pnl / len(windows):.4f}")
        print(f"Annualized (96 windows/day): ${total_pnl / len(windows) * 96 * 365:.2f}")
        
        print(f"\nEdge Distribution:")
        for edge in sorted(edge_dist.keys()):
            count = edge_dist[edge]
            print(f"  {edge}c edge: {count} trades ({count/total_trades*100:.1f}%)")
    else:
        print("No trades found!")
    
    # Save results
    outdir = "out_hybrid_backtest"
    os.makedirs(outdir, exist_ok=True)
    
    # Write trades CSV
    with open(os.path.join(outdir, "trades.csv"), "w", newline="") as f:
        writer = csv.writer(f)
        writer.writerow([
            "window_id", "strategy", "up_price", "down_price", 
            "combined_cost", "edge", "pnl"
        ])
        for t in all_trades:
            writer.writerow([
                t.window_id, t.strategy, t.up_entry_price, t.down_entry_price,
                t.combined_cost, t.theoretical_edge, t.pnl
            ])
    
    # Write summary
    summary = {
        "config": {
            "max_combined_cost": max_combined_cost,
            "min_edge_cents": min_edge,
            "size_per_trade": size
        },
        "results": {
            "total_windows": len(windows),
            "total_trades": total_trades,
            "hit_rate": total_trades / len(windows) if windows else 0,
            "total_pnl": total_pnl,
            "pnl_per_window": total_pnl / len(windows) if windows else 0,
            "avg_edge": avg_edge if total_trades else 0,
            "edge_distribution": edge_dist
        }
    }
    
    with open(os.path.join(outdir, "summary.json"), "w") as f:
        json.dump(summary, f, indent=2)
    
    print(f"\nResults saved to {outdir}/")
    
    return all_trades, summary


if __name__ == "__main__":
    import sys
    
    max_cost = int(sys.argv[1]) if len(sys.argv) > 1 else 99
    min_edge = int(sys.argv[2]) if len(sys.argv) > 2 else 1
    
    run_hybrid_backtest(max_cost, min_edge)

