"""
PROPER 50-DAY ANALYSIS: All 4,872 Windows
Fixed timestamp parsing to handle real clock time
"""

import os
import re
from pathlib import Path
from dataclasses import dataclass, field
from typing import List, Tuple, Optional
import statistics

DATA_DIR = Path("C:/Users/karthick/Documents/tmp/backtesting15mbitcoin")

@dataclass 
class WindowStats:
    window_id: str
    ticks: int = 0
    
    # Price data  
    yes_prices: List[float] = field(default_factory=list)
    no_prices: List[float] = field(default_factory=list)
    
    # Computed from both buy/sell
    spreads: List[float] = field(default_factory=list)
    mids: List[float] = field(default_factory=list)
    
    # Time in zones
    time_balanced: int = 0
    time_mid_zone: int = 0
    time_extreme: int = 0
    time_reward_zone: int = 0
    
    # Volatility
    moves_5s: List[float] = field(default_factory=list)
    
    # Full-set
    fullset_opps: int = 0
    fullset_edges: List[float] = field(default_factory=list)

def parse_window_times(window_id: str) -> Tuple[int, int]:
    """Parse window ID to get start/end minute within hour.
    Format: YY_MM_DD_HH_StartMM_HH_EndMM
    Example: 25_12_06_10_15_10_30 -> start=15, end=30
    """
    parts = window_id.split('_')
    if len(parts) >= 7:
        start_min = int(parts[4])
        end_min = int(parts[6])
        return start_min, end_min
    return 0, 15

def parse_line(line: str, window_start_min: int) -> Tuple[float, int, int] | None:
    """Parse tick line with proper timestamp handling.
    Timestamps are MM:SS:mmm where MM is minute within the hour.
    """
    pattern = r'(\d{2}):(\d{2}):(\d{3})\s*-\s*UP\s+(-?\d+)C\s*\|\s*DOWN\s+(-?\d+)C'
    m = re.match(pattern, line.strip())
    if not m:
        return None
    
    mins, secs, ms = int(m.group(1)), int(m.group(2)), int(m.group(3))
    yes_c, no_c = int(m.group(4)), int(m.group(5))
    
    # -1 is settlement marker
    if yes_c == -1 or no_c == -1:
        return None
    
    # Calculate elapsed seconds from window start
    # Handle hour wrap (e.g., window 45-00 where 00 means next hour)
    if mins < window_start_min and window_start_min >= 45:
        # Next hour case: e.g., start=45, current=02 means 17 mins elapsed
        elapsed_mins = (60 - window_start_min) + mins
    else:
        elapsed_mins = mins - window_start_min
    
    elapsed = elapsed_mins * 60 + secs + ms / 1000.0
    
    # Valid window is 15 minutes = 900 seconds (+ small buffer)
    if elapsed < 0 or elapsed > 920:
        return None
    
    return (elapsed, yes_c, no_c)

def load_window(window_id: str) -> Optional[WindowStats]:
    """Load a single window with proper parsing."""
    buy_file = DATA_DIR / "market_logs" / window_id / f"{window_id}.txt"
    sell_file = DATA_DIR / "market_logs_sell" / window_id / f"{window_id}.txt"
    
    if not buy_file.exists():
        return None
    
    # Parse window times from ID
    start_min, end_min = parse_window_times(window_id)
    
    stats = WindowStats(window_id=window_id)
    
    # Parse buy side (asks)
    buy_ticks = []
    try:
        with open(buy_file, 'r', encoding='utf-8', errors='ignore') as f:
            for line in f:
                result = parse_line(line, start_min)
                if result:
                    buy_ticks.append(result)
    except Exception:
        return None
    
    # Parse sell side (bids)
    sell_ticks = []
    if sell_file.exists():
        try:
            with open(sell_file, 'r', encoding='utf-8', errors='ignore') as f:
                for line in f:
                    result = parse_line(line, start_min)
                    if result:
                        sell_ticks.append(result)
        except Exception:
            pass
    
    if len(buy_ticks) < 3:
        return None
    
    stats.ticks = len(buy_ticks)
    
    # Build timeline
    buy_dict = {t: (y, n) for t, y, n in buy_ticks}
    sell_dict = {t: (y, n) for t, y, n in sell_ticks}
    all_times = sorted(set(buy_dict.keys()) | set(sell_dict.keys()))
    
    last_ask_y, last_ask_n = None, None
    last_bid_y, last_bid_n = None, None
    prev_mids = []
    
    for t in all_times:
        if t in buy_dict:
            last_ask_y, last_ask_n = buy_dict[t]
        if t in sell_dict:
            last_bid_y, last_bid_n = sell_dict[t]
        
        if last_ask_y is None:
            continue
        
        # Convert to decimal (0-1)
        ask_y = last_ask_y / 100.0
        ask_n = last_ask_n / 100.0
        
        # Skip if both are 0 (no market data)
        if ask_y <= 0 and ask_n <= 0:
            continue
        
        stats.yes_prices.append(ask_y)
        stats.no_prices.append(ask_n)
        
        # Calculate mid and spread
        if last_bid_y is not None and last_bid_y > 0:
            bid_y = last_bid_y / 100.0
            bid_n = last_bid_n / 100.0
            
            spread_y = max(0, ask_y - bid_y)
            spread_n = max(0, ask_n - bid_n)
            avg_spread = (spread_y + spread_n) / 2
            stats.spreads.append(avg_spread)
            
            if avg_spread <= 0.03:
                stats.time_reward_zone += 1
            
            mid_y = (ask_y + bid_y) / 2
        else:
            mid_y = ask_y
        
        stats.mids.append(mid_y)
        
        # Zone classification
        if 0.35 <= mid_y <= 0.65:
            stats.time_balanced += 1
        if 0.45 <= mid_y <= 0.55:
            stats.time_mid_zone += 1
        if mid_y < 0.10 or mid_y > 0.90:
            stats.time_extreme += 1
        
        # Volatility (5s moves)
        prev_mids.append((t, mid_y))
        for pt, pm in reversed(prev_mids[:-1]):
            if t - pt >= 4.5:
                move = abs(mid_y - pm)
                stats.moves_5s.append(move)
                break
        if len(prev_mids) > 25:
            prev_mids = prev_mids[-20:]
        
        # Full-set check
        total_cost = ask_y + ask_n
        if 0 < total_cost < 0.99:
            stats.fullset_opps += 1
            stats.fullset_edges.append((1.0 - total_cost) * 100)
    
    return stats if len(stats.yes_prices) >= 3 else None

def main():
    print("=" * 80)
    print("POLYMARKET BTC 15-MIN: COMPLETE 50-DAY ANALYSIS")
    print("=" * 80)
    print()
    
    market_logs = DATA_DIR / "market_logs"
    if not market_logs.exists():
        print(f"ERROR: {market_logs} not found")
        return
    
    window_dirs = sorted([d.name for d in market_logs.iterdir() if d.is_dir()])
    print(f"Total window directories: {len(window_dirs)}")
    
    # Load all windows
    all_stats = []
    failed_windows = []
    
    for i, wid in enumerate(window_dirs):
        if (i + 1) % 1000 == 0:
            print(f"  Processing {i+1}/{len(window_dirs)}...")
        
        stats = load_window(wid)
        if stats:
            all_stats.append(stats)
        else:
            failed_windows.append(wid)
    
    print(f"\nSuccessfully loaded: {len(all_stats)} windows")
    print(f"Failed/empty: {len(failed_windows)} windows")
    
    # Show some failed examples
    if failed_windows:
        print(f"\nSample failed windows: {failed_windows[:5]}")
    
    if not all_stats:
        print("No valid data!")
        return
    
    # Aggregate
    total_ticks = sum(len(s.yes_prices) for s in all_stats)
    total_balanced = sum(s.time_balanced for s in all_stats)
    total_mid = sum(s.time_mid_zone for s in all_stats)
    total_extreme = sum(s.time_extreme for s in all_stats)
    total_reward = sum(s.time_reward_zone for s in all_stats)
    
    print("\n" + "=" * 80)
    print("SECTION 1: MARKET CONDITIONS")
    print("=" * 80)
    
    print(f"\nTotal valid ticks: {total_ticks:,}")
    print(f"Average ticks per window: {total_ticks / len(all_stats):.0f}")
    
    print(f"\nTime Distribution:")
    print(f"  Balanced (0.35-0.65):     {total_balanced:,} ({100*total_balanced/total_ticks:.1f}%)")
    print(f"  Mid-zone (0.45-0.55):     {total_mid:,} ({100*total_mid/total_ticks:.1f}%) <- BEST REBATES")
    print(f"  Extreme (<0.10/>0.90):    {total_extreme:,} ({100*total_extreme/total_ticks:.1f}%)")
    print(f"  Reward zone (spread<=3c): {total_reward:,} ({100*total_reward/total_ticks:.1f}%)")
    
    # Per-window
    windows_balanced = sum(1 for s in all_stats if s.time_balanced > len(s.yes_prices) * 0.5)
    windows_mid = sum(1 for s in all_stats if s.time_mid_zone > len(s.yes_prices) * 0.3)
    windows_extreme = sum(1 for s in all_stats if s.time_extreme > len(s.yes_prices) * 0.5)
    
    print(f"\nPer-Window ({len(all_stats)} windows):")
    print(f"  Mostly balanced (>50%): {windows_balanced} ({100*windows_balanced/len(all_stats):.1f}%)")
    print(f"  Significant mid (>30%): {windows_mid} ({100*windows_mid/len(all_stats):.1f}%)")
    print(f"  Mostly extreme (>50%):  {windows_extreme} ({100*windows_extreme/len(all_stats):.1f}%)")
    
    # Spread
    print("\n" + "=" * 80)
    print("SECTION 2: SPREAD ANALYSIS")
    print("=" * 80)
    
    all_spreads = []
    for s in all_stats:
        all_spreads.extend(s.spreads)
    
    if all_spreads:
        spread_cents = sorted([x * 100 for x in all_spreads])
        n = len(spread_cents)
        
        print(f"\nSpread Distribution (cents) - {n:,} observations:")
        print(f"  P10:  {spread_cents[n//10]:.1f}c")
        print(f"  P25:  {spread_cents[n//4]:.1f}c")
        print(f"  P50:  {spread_cents[n//2]:.1f}c (median)")
        print(f"  P75:  {spread_cents[3*n//4]:.1f}c")
        print(f"  P90:  {spread_cents[9*n//10]:.1f}c")
        
        tight = sum(1 for x in spread_cents if x <= 1) / n
        reward = sum(1 for x in spread_cents if x <= 3) / n
        print(f"\n  Tight (<=1c):       {100*tight:.1f}%")
        print(f"  Reward zone (<=3c): {100*reward:.1f}%")
    
    # Volatility
    print("\n" + "=" * 80)
    print("SECTION 3: VOLATILITY")
    print("=" * 80)
    
    all_moves = []
    for s in all_stats:
        all_moves.extend(s.moves_5s)
    
    if all_moves:
        moves_cents = sorted([x * 100 for x in all_moves])
        n = len(moves_cents)
        
        print(f"\n5-Second Move Distribution (cents) - {n:,} observations:")
        print(f"  P50:  {moves_cents[n//2]:.2f}c")
        print(f"  P75:  {moves_cents[3*n//4]:.2f}c")
        print(f"  P90:  {moves_cents[9*n//10]:.2f}c")
        print(f"  P95:  {moves_cents[19*n//20]:.2f}c")
        print(f"  P99:  {moves_cents[99*n//100]:.2f}c")
        
        low_vol = sum(1 for x in moves_cents if x <= 5) / n
        spike = sum(1 for x in moves_cents if x > 10) / n
        print(f"\n  Low vol (<=5c): {100*low_vol:.1f}%")
        print(f"  Spike (>10c):   {100*spike:.1f}%")
    
    # Full-set
    print("\n" + "=" * 80)
    print("SECTION 4: FULL-SET ARBITRAGE")
    print("=" * 80)
    
    total_fullset = sum(s.fullset_opps for s in all_stats)
    all_edges = []
    for s in all_stats:
        all_edges.extend(s.fullset_edges)
    
    print(f"\nFull-set opportunities (YES+NO < 99c): {total_fullset:,}")
    print(f"Frequency: {100*total_fullset/total_ticks:.3f}%")
    if all_edges:
        edges_sorted = sorted(all_edges)
        print(f"Edge distribution (cents):")
        print(f"  Mean:   {statistics.mean(all_edges):.2f}c")
        print(f"  Median: {statistics.median(all_edges):.2f}c")
        print(f"  P90:    {edges_sorted[9*len(edges_sorted)//10]:.2f}c")
    
    # Maker rebates
    print("\n" + "=" * 80)
    print("SECTION 5: MAKER REBATE ANALYSIS")
    print("=" * 80)
    
    print("""
TAKER FEE STRUCTURE (= Maker Rebate Pool):
  Price   Fee/100sh   Rate
  10c     $0.20       0.20%
  30c     $1.10       1.10%
  50c     $1.56       1.56% <- MAXIMUM
  70c     $1.10       1.10%
  90c     $0.20       0.20%
""")
    
    # Estimate time in each rebate zone
    all_mids = []
    for s in all_stats:
        all_mids.extend(s.mids)
    
    if all_mids:
        zone_10_20 = sum(1 for m in all_mids if 0.10 <= m < 0.20 or 0.80 < m <= 0.90) / len(all_mids)
        zone_20_30 = sum(1 for m in all_mids if 0.20 <= m < 0.30 or 0.70 < m <= 0.80) / len(all_mids)
        zone_30_40 = sum(1 for m in all_mids if 0.30 <= m < 0.40 or 0.60 < m <= 0.70) / len(all_mids)
        zone_40_50 = sum(1 for m in all_mids if 0.40 <= m < 0.50 or 0.50 < m <= 0.60) / len(all_mids)
        zone_50 = sum(1 for m in all_mids if 0.48 <= m <= 0.52) / len(all_mids)
        
        print("Time in Rebate Zones:")
        print(f"  Near 50c (48-52c): {100*zone_50:.1f}% <- Max rebates")
        print(f"  40-60c range:      {100*zone_40_50:.1f}%")
        print(f"  30-70c range:      {100*zone_30_40:.1f}%")
        print(f"  20-80c range:      {100*zone_20_30:.1f}%")
        print(f"  10-90c range:      {100*zone_10_20:.1f}%")
    
    # Parameters
    print("\n" + "=" * 80)
    print("SECTION 6: OPTIMAL PARAMETERS")
    print("=" * 80)
    
    spike_p90 = sorted([x*100 for x in all_moves])[9*len(all_moves)//10] if all_moves else 5
    spike_p95 = sorted([x*100 for x in all_moves])[19*len(all_moves)//20] if all_moves else 8
    
    print(f"""
DATA-DRIVEN CONFIG:

# When to trade
MM_ENTRY_MID_MIN=0.35
MM_ENTRY_MID_MAX=0.65
MM_MIN_SPREAD_CENTS=1

# Volatility (from data)
MM_SPIKE_THRESHOLD_CENTS={spike_p90:.0f}   # P90
MM_VOL_10S_CENTS={spike_p95:.0f}           # P95
MM_SPIKE_COOLDOWN_SECS=10

# Position sizing ($15-20 account)
MM_QUOTE_SIZE=5
MM_MAX_USDC_LOCKED=2.00
MM_MAX_SHARES_PER_TOKEN=20

# Timing
OPENING_MODE_SECS=30
MM_ENTRY_CUTOFF_SECS=180
MM_FLATTEN_DEADLINE_SECS=120

# Safety
MM_EXIT_ENFORCED=1
MM_EMERGENCY_TAKER_EXIT=1
""")
    
    # Summary
    print("\n" + "=" * 80)
    print("SECTION 7: STRATEGY SUMMARY")
    print("=" * 80)
    
    balanced_pct = 100*total_balanced/total_ticks if total_ticks > 0 else 0
    mid_pct = 100*total_mid/total_ticks if total_ticks > 0 else 0
    
    print(f"""
MAKER REBATE HARVESTING STRATEGY:

1. WHEN TO TRADE:
   - {balanced_pct:.1f}% of time market is tradeable (35-65c)
   - {mid_pct:.1f}% of time in high-rebate zone (45-55c)

2. HOW TO TRADE:
   - Place postOnly bids near touch
   - Get filled as maker -> earn rebates
   - Exit at break-even; rebate is the profit

3. EXPECTED RETURNS:
   - Rebate per 5-share fill at 50c: ~$0.08
   - Target 5-20 fills/day
   - Daily estimate: $0.40-$1.60 rebates

4. RISK:
   - Max $2 locked at any time
   - Emergency taker exit if stuck
   - Flatten before settlement

FULL-SET ARB: Only {100*total_fullset/total_ticks:.3f}% opportunity - NOT VIABLE
FOCUS: MAKER REBATES
""")
    
    print("=" * 80)
    print("ANALYSIS COMPLETE")
    print("=" * 80)

if __name__ == "__main__":
    main()

