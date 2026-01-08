"""
FULL 50-DAY ANALYSIS: 4,872 Windows
Polymarket BTC 15-min Reward Opportunity Analysis
"""

import os
import re
from pathlib import Path
from dataclasses import dataclass, field
from typing import List, Tuple, Optional
from collections import defaultdict
import statistics

DATA_DIR = Path("C:/Users/karthick/Documents/tmp/backtesting15mbitcoin")

@dataclass
class WindowStats:
    window_id: str
    ticks: int = 0
    valid_ticks: int = 0  # Ticks with non-zero prices
    
    # Price data
    yes_asks: List[float] = field(default_factory=list)  # Buy prices
    no_asks: List[float] = field(default_factory=list)
    yes_bids: List[float] = field(default_factory=list)  # Sell prices
    no_bids: List[float] = field(default_factory=list)
    
    # Time in zones (only valid ticks)
    time_balanced: int = 0      # 0.35-0.65
    time_mid_zone: int = 0      # 0.45-0.55
    time_extreme: int = 0       # <0.10 or >0.90
    time_reward_zone: int = 0   # spread <= 3c
    
    # Spreads
    spreads: List[float] = field(default_factory=list)
    
    # Volatility
    moves_5s: List[float] = field(default_factory=list)
    
    # Full-set opportunities
    fullset_opps: int = 0
    fullset_edges: List[float] = field(default_factory=list)

def parse_line(line: str) -> Tuple[float, int, int] | None:
    """Parse: MM:SS:mmm - UP XXC | DOWN YYC"""
    pattern = r'(\d{2}):(\d{2}):(\d{3})\s*-\s*UP\s+(-?\d+)C\s*\|\s*DOWN\s+(-?\d+)C'
    m = re.match(pattern, line.strip())
    if not m:
        return None
    
    mins, secs, ms = int(m.group(1)), int(m.group(2)), int(m.group(3))
    yes_c, no_c = int(m.group(4)), int(m.group(5))
    
    # Only skip if -1 (settlement marker)
    if yes_c == -1 or no_c == -1:
        return None
    
    elapsed = mins * 60 + secs + ms / 1000.0
    if elapsed > 901:
        return None
    
    return (elapsed, yes_c, no_c)

def load_window(window_id: str) -> Optional[WindowStats]:
    """Load a single window's buy and sell data"""
    buy_dir = DATA_DIR / "market_logs" / window_id
    sell_dir = DATA_DIR / "market_logs_sell" / window_id
    
    buy_file = buy_dir / f"{window_id}.txt"
    sell_file = sell_dir / f"{window_id}.txt"
    
    if not buy_file.exists():
        return None
    
    stats = WindowStats(window_id=window_id)
    
    # Parse buy side (asks)
    buy_ticks = []
    try:
        with open(buy_file, 'r', encoding='utf-8', errors='ignore') as f:
            prev_t = -1
            for line in f:
                result = parse_line(line)
                if result is None:
                    continue
                t, yes_c, no_c = result
                if t < prev_t:  # Time reset = contamination
                    break
                prev_t = t
                buy_ticks.append((t, yes_c, no_c))
    except Exception:
        return None
    
    # Parse sell side (bids)
    sell_ticks = []
    if sell_file.exists():
        try:
            with open(sell_file, 'r', encoding='utf-8', errors='ignore') as f:
                prev_t = -1
                for line in f:
                    result = parse_line(line)
                    if result is None:
                        continue
                    t, yes_c, no_c = result
                    if t < prev_t:
                        break
                    prev_t = t
                    sell_ticks.append((t, yes_c, no_c))
        except Exception:
            pass
    
    if len(buy_ticks) < 5:
        return None
    
    stats.ticks = len(buy_ticks)
    
    # Build merged timeline
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
        
        # Convert to decimal
        ask_y = last_ask_y / 100.0
        ask_n = last_ask_n / 100.0
        
        # Skip truly invalid (both 0)
        if ask_y == 0 and ask_n == 0:
            continue
        
        stats.valid_ticks += 1
        stats.yes_asks.append(ask_y)
        stats.no_asks.append(ask_n)
        
        # Calculate mid
        if last_bid_y is not None:
            bid_y = last_bid_y / 100.0
            bid_n = last_bid_n / 100.0
            stats.yes_bids.append(bid_y)
            stats.no_bids.append(bid_n)
            
            # Spread = ask - bid
            spread_y = ask_y - bid_y
            spread_n = ask_n - bid_n
            avg_spread = (spread_y + spread_n) / 2
            if avg_spread >= 0:  # Valid spread
                stats.spreads.append(avg_spread)
                if avg_spread <= 0.03:
                    stats.time_reward_zone += 1
            
            mid_y = (ask_y + bid_y) / 2
        else:
            mid_y = ask_y
        
        # Zone classification
        if 0.35 <= mid_y <= 0.65:
            stats.time_balanced += 1
        if 0.45 <= mid_y <= 0.55:
            stats.time_mid_zone += 1
        if mid_y < 0.10 or mid_y > 0.90:
            stats.time_extreme += 1
        
        # Volatility (5s moves)
        prev_mids.append((t, mid_y))
        # Find mid from ~5s ago
        for pt, pm in reversed(prev_mids[:-1]):
            if t - pt >= 4.5:
                move = abs(mid_y - pm)
                stats.moves_5s.append(move)
                break
        # Keep last 20
        if len(prev_mids) > 20:
            prev_mids = prev_mids[-15:]
        
        # Full-set check (both asks available)
        total_cost = ask_y + ask_n
        if total_cost < 0.99 and total_cost > 0:
            stats.fullset_opps += 1
            stats.fullset_edges.append((1.0 - total_cost) * 100)
    
    return stats if stats.valid_ticks >= 5 else None

def main():
    print("=" * 80)
    print("POLYMARKET BTC 15-MIN: FULL 50-DAY ANALYSIS (4,872 Windows)")
    print("=" * 80)
    print()
    
    # Get all window directories
    market_logs = DATA_DIR / "market_logs"
    if not market_logs.exists():
        print(f"ERROR: {market_logs} not found")
        return
    
    window_dirs = sorted([d.name for d in market_logs.iterdir() if d.is_dir()])
    print(f"Found {len(window_dirs)} window directories")
    
    # Load all windows
    all_stats = []
    skipped = 0
    for i, wid in enumerate(window_dirs):
        if (i + 1) % 500 == 0:
            print(f"  Processing {i+1}/{len(window_dirs)}...")
        stats = load_window(wid)
        if stats:
            all_stats.append(stats)
        else:
            skipped += 1
    
    print(f"\nLoaded: {len(all_stats)} windows")
    print(f"Skipped: {skipped} windows (empty/invalid)")
    
    if not all_stats:
        print("No valid data!")
        return
    
    # Aggregate stats
    total_ticks = sum(s.ticks for s in all_stats)
    total_valid = sum(s.valid_ticks for s in all_stats)
    total_balanced = sum(s.time_balanced for s in all_stats)
    total_mid = sum(s.time_mid_zone for s in all_stats)
    total_extreme = sum(s.time_extreme for s in all_stats)
    total_reward = sum(s.time_reward_zone for s in all_stats)
    
    # =========================================================================
    print("\n" + "=" * 80)
    print("SECTION 1: MARKET CONDITIONS")
    print("=" * 80)
    
    print(f"\nTotal ticks: {total_ticks:,}")
    print(f"Valid ticks (non-zero prices): {total_valid:,}")
    
    if total_valid > 0:
        print(f"\nTime Distribution (of valid ticks):")
        print(f"  Balanced (0.35-0.65):     {total_balanced:,} ({100*total_balanced/total_valid:.1f}%)")
        print(f"  Mid-zone (0.45-0.55):     {total_mid:,} ({100*total_mid/total_valid:.1f}%) <- BEST REBATES")
        print(f"  Extreme (<0.10/>0.90):    {total_extreme:,} ({100*total_extreme/total_valid:.1f}%)")
        print(f"  Reward zone (spread<=3c): {total_reward:,} ({100*total_reward/total_valid:.1f}%)")
    
    # Per-window stats
    windows_balanced = sum(1 for s in all_stats if s.time_balanced > s.valid_ticks * 0.5)
    windows_mid = sum(1 for s in all_stats if s.time_mid_zone > s.valid_ticks * 0.3)
    windows_extreme = sum(1 for s in all_stats if s.time_extreme > s.valid_ticks * 0.5)
    
    print(f"\nPer-Window ({len(all_stats)} windows):")
    print(f"  Mostly balanced (>50%): {windows_balanced} ({100*windows_balanced/len(all_stats):.1f}%)")
    print(f"  Significant mid (>30%): {windows_mid} ({100*windows_mid/len(all_stats):.1f}%)")
    print(f"  Mostly extreme (>50%):  {windows_extreme} ({100*windows_extreme/len(all_stats):.1f}%)")
    
    # =========================================================================
    print("\n" + "=" * 80)
    print("SECTION 2: SPREAD ANALYSIS")
    print("=" * 80)
    
    all_spreads = []
    for s in all_stats:
        all_spreads.extend(s.spreads)
    
    if all_spreads:
        spread_cents = [x * 100 for x in all_spreads]
        spread_cents.sort()
        n = len(spread_cents)
        
        print(f"\nSpread Distribution (cents) - {n:,} observations:")
        print(f"  P10:    {spread_cents[n//10]:.1f}c")
        print(f"  P25:    {spread_cents[n//4]:.1f}c")
        print(f"  P50:    {spread_cents[n//2]:.1f}c")
        print(f"  P75:    {spread_cents[3*n//4]:.1f}c")
        print(f"  P90:    {spread_cents[9*n//10]:.1f}c")
        
        tight = sum(1 for x in spread_cents if x <= 1) / n
        reward = sum(1 for x in spread_cents if x <= 3) / n
        print(f"\n  Tight (<=1c): {100*tight:.1f}%")
        print(f"  Reward zone (<=3c): {100*reward:.1f}%")
    
    # =========================================================================
    print("\n" + "=" * 80)
    print("SECTION 3: VOLATILITY")
    print("=" * 80)
    
    all_moves = []
    for s in all_stats:
        all_moves.extend(s.moves_5s)
    
    if all_moves:
        moves_cents = [x * 100 for x in all_moves]
        moves_cents.sort()
        n = len(moves_cents)
        
        print(f"\n5-Second Move Distribution (cents) - {n:,} observations:")
        print(f"  P50:  {moves_cents[n//2]:.2f}c")
        print(f"  P75:  {moves_cents[3*n//4]:.2f}c")
        print(f"  P90:  {moves_cents[9*n//10]:.2f}c")
        print(f"  P95:  {moves_cents[19*n//20]:.2f}c")
        print(f"  P99:  {moves_cents[99*n//100]:.2f}c")
        
        low_vol = sum(1 for x in moves_cents if x <= 5) / n
        spike = sum(1 for x in moves_cents if x > 10) / n
        print(f"\n  Low vol (<=5c): {100*low_vol:.1f}% <- SAFE")
        print(f"  Spike (>10c):   {100*spike:.1f}% <- PAUSE")
    
    # =========================================================================
    print("\n" + "=" * 80)
    print("SECTION 4: FULL-SET ARBITRAGE")
    print("=" * 80)
    
    total_fullset = sum(s.fullset_opps for s in all_stats)
    all_edges = []
    for s in all_stats:
        all_edges.extend(s.fullset_edges)
    
    print(f"\nFull-set opportunities (YES+NO < 99c): {total_fullset:,}")
    if total_valid > 0:
        print(f"Frequency: {100*total_fullset/total_valid:.2f}% of valid ticks")
    if all_edges:
        print(f"Average edge: {statistics.mean(all_edges):.2f}c")
        print(f"Median edge:  {statistics.median(all_edges):.2f}c")
    
    # =========================================================================
    print("\n" + "=" * 80)
    print("SECTION 5: MAKER REBATE POTENTIAL")
    print("=" * 80)
    
    print("""
TAKER FEE (= MAKER REBATE POOL) BY PRICE:
  Price   Fee/100sh   Effective Rate
  10c     $0.20       0.20%
  20c     $0.64       0.64%
  30c     $1.10       1.10%
  40c     $1.44       1.44%
  50c     $1.56       1.56%  <- MAXIMUM
  60c     $1.44       1.44%
  70c     $1.10       1.10%
  80c     $0.64       0.64%
  90c     $0.20       0.20%
""")
    
    # Estimate rebate opportunity
    print("REBATE OPPORTUNITY ESTIMATE:")
    print(f"  - {100*total_mid/total_valid:.1f}% of time in high-rebate zone (45-55c)")
    print(f"  - {100*total_balanced/total_valid:.1f}% of time in tradeable zone (35-65c)")
    print(f"  - Your share of rebates depends on your fill volume vs other makers")
    
    # =========================================================================
    print("\n" + "=" * 80)
    print("SECTION 6: RECOMMENDED PARAMETERS")
    print("=" * 80)
    
    # Data-driven thresholds
    if all_moves:
        spike_p90 = sorted([x*100 for x in all_moves])[9*len(all_moves)//10]
        spike_p95 = sorted([x*100 for x in all_moves])[19*len(all_moves)//20]
    else:
        spike_p90, spike_p95 = 5, 8
    
    if all_spreads:
        spread_p50 = sorted([x*100 for x in all_spreads])[len(all_spreads)//2]
    else:
        spread_p50 = 2
    
    print(f"""
DATA-DRIVEN CONFIG FOR $15-20 ACCOUNT:

# Regime (when to trade)
MM_ENTRY_MID_MIN=0.35
MM_ENTRY_MID_MAX=0.65
MM_MIN_SPREAD_CENTS=1

# Volatility (spike detection)
MM_SPIKE_THRESHOLD_CENTS={spike_p90:.0f}   # P90 of 5s moves
MM_SPIKE_COOLDOWN_SECS=10
MM_VOL_10S_CENTS={spike_p95:.0f}           # P95 threshold

# Position sizing
MM_QUOTE_SIZE=5              # Minimum shares
MM_MAX_USDC_LOCKED=2.00      # Max capital at risk
MM_MAX_SHARES_PER_TOKEN=20

# Timing
OPENING_MODE_SECS=30
MM_ENTRY_CUTOFF_SECS=180     # No entries last 3 min
MM_FLATTEN_DEADLINE_SECS=120 # Force exit last 2 min

# Safety
MM_EXIT_ENFORCED=1
MM_EMERGENCY_TAKER_EXIT=1
""")
    
    # =========================================================================
    print("\n" + "=" * 80)
    print("SECTION 7: STRATEGY SUMMARY")
    print("=" * 80)
    
    print("""
MAKER REBATE HARVESTING STRATEGY:

1. WHEN: Only trade in balanced zone (35-65c)
   - {balanced_pct:.1f}% of time is tradeable

2. HOW: Place postOnly bids near touch
   - Get filled as maker -> earn rebates
   - Exit at break-even or tiny profit
   - Rebate is the real edge

3. RISK: Strict limits
   - Max $2 locked at any time
   - Single position per side
   - Time-based exits before settlement

4. EXPECTED:
   - 5-20 fills per day (depends on activity)
   - $0.04-0.16 rebate per 5-share fill at 50c
   - Daily target: $0.50-$3.00 in rebates

KEY INSIGHT:
The goal is NOT spread capture (often only 1c).
The goal is to GET FILLED AS A MAKER and earn rebates.
""".format(balanced_pct=100*total_balanced/total_valid if total_valid > 0 else 0))
    
    print("=" * 80)
    print("ANALYSIS COMPLETE")
    print("=" * 80)

if __name__ == "__main__":
    main()

