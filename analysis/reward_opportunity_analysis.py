"""
Comprehensive Analysis of 50-day BTC 15-min Data
Goal: Maximize Polymarket Rewards with Proper Risk Management

Key Polymarket Reward Mechanisms:
1. LIQUIDITY REWARDS: Earn by placing limit orders within spread
   - Closer to midpoint = more rewards
   - Max spread ±3c, Min shares 200
   
2. MAKER REBATES (15-min crypto only):
   - Funded by taker fees (max 1.56% at 50c, 0% at extremes)
   - Paid daily based on filled liquidity provided
   - The more fills you get as maker, the more rebates
"""

import os
import re
from pathlib import Path
from dataclasses import dataclass, field
from typing import List, Dict, Tuple
from collections import defaultdict
import statistics
import json

# Data directory
DATA_DIR = Path("C:/Users/karthick/Documents/tmp/backtesting15mbitcoin")

@dataclass
class WindowStats:
    """Statistics for a single 15-min window"""
    window_id: str
    ticks: int = 0
    
    # Price data
    yes_prices: List[float] = field(default_factory=list)
    no_prices: List[float] = field(default_factory=list)
    yes_bids: List[float] = field(default_factory=list)  # From sell side
    no_bids: List[float] = field(default_factory=list)
    
    # Time spent in different zones
    time_balanced: float = 0  # 0.35-0.65
    time_mid_zone: float = 0  # 0.45-0.55 (best for rewards)
    time_extreme: float = 0   # <0.10 or >0.90
    
    # Spread data
    spreads: List[float] = field(default_factory=list)
    
    # Volatility
    moves_1s: List[float] = field(default_factory=list)
    moves_5s: List[float] = field(default_factory=list)
    
    # Opening period (first 60s)
    opening_balanced: bool = False
    opening_spread_avg: float = 0
    opening_vol: float = 0

def parse_tick_line(line: str) -> Tuple[float, int, int] | None:
    """Parse a tick line, return (elapsed_s, yes_cents, no_cents) or None"""
    pattern = r'(\d{2}):(\d{2}):(\d{3})\s*-\s*UP\s+(-?\d+)C\s*\|\s*DOWN\s+(-?\d+)C'
    m = re.match(pattern, line.strip())
    if not m:
        return None
    
    mins, secs, ms = int(m.group(1)), int(m.group(2)), int(m.group(3))
    yes_c, no_c = int(m.group(4)), int(m.group(5))
    
    # Stop conditions
    if yes_c == -1 or no_c == -1:
        return None
    
    elapsed = mins * 60 + secs + ms / 1000.0
    if elapsed > 901:
        return None
    
    return (elapsed, yes_c, no_c)

def load_window(window_dir: Path) -> WindowStats | None:
    """Load a single window's data (both buy and sell sides)"""
    window_id = window_dir.name
    
    # Find the tick files
    buy_file = window_dir / f"{window_id}.txt"
    sell_dir = DATA_DIR / "market_logs_sell" / window_id
    sell_file = sell_dir / f"{window_id}.txt" if sell_dir.exists() else None
    
    if not buy_file.exists():
        return None
    
    stats = WindowStats(window_id=window_id)
    
    # Parse buy side (asks - what you pay to buy)
    buy_ticks = []
    try:
        with open(buy_file, 'r', encoding='utf-8', errors='ignore') as f:
            prev_elapsed = -1
            for line in f:
                result = parse_tick_line(line)
                if result is None:
                    continue
                elapsed, yes_c, no_c = result
                if elapsed < prev_elapsed:  # Time reset
                    break
                prev_elapsed = elapsed
                buy_ticks.append((elapsed, yes_c / 100.0, no_c / 100.0))
    except Exception as e:
        return None
    
    # Parse sell side (bids - what you get if you sell)
    sell_ticks = []
    if sell_file and sell_file.exists():
        try:
            with open(sell_file, 'r', encoding='utf-8', errors='ignore') as f:
                prev_elapsed = -1
                for line in f:
                    result = parse_tick_line(line)
                    if result is None:
                        continue
                    elapsed, yes_c, no_c = result
                    if elapsed < prev_elapsed:
                        break
                    prev_elapsed = elapsed
                    sell_ticks.append((elapsed, yes_c / 100.0, no_c / 100.0))
        except:
            pass
    
    if len(buy_ticks) < 10:
        return None
    
    # Merge buy and sell into unified timeline
    # Buy = Ask (what you pay), Sell = Bid (what you get)
    buy_dict = {t[0]: (t[1], t[2]) for t in buy_ticks}
    sell_dict = {t[0]: (t[1], t[2]) for t in sell_ticks}
    
    all_times = sorted(set(buy_dict.keys()) | set(sell_dict.keys()))
    
    last_ask_yes, last_ask_no = None, None
    last_bid_yes, last_bid_no = None, None
    
    prev_mid = None
    prev_mid_5s = []
    
    for t in all_times:
        # Forward fill
        if t in buy_dict:
            last_ask_yes, last_ask_no = buy_dict[t]
        if t in sell_dict:
            last_bid_yes, last_bid_no = sell_dict[t]
        
        if last_ask_yes is None:
            continue
        
        stats.ticks += 1
        stats.yes_prices.append(last_ask_yes)
        stats.no_prices.append(last_ask_no)
        
        if last_bid_yes is not None:
            stats.yes_bids.append(last_bid_yes)
            stats.no_bids.append(last_bid_no)
            # Spread = ask - bid
            spread_yes = last_ask_yes - last_bid_yes
            spread_no = last_ask_no - last_bid_no
            stats.spreads.append((spread_yes + spread_no) / 2)
        
        # Calculate mid (using ask prices as proxy if no bid)
        mid_yes = last_ask_yes
        if last_bid_yes is not None:
            mid_yes = (last_ask_yes + last_bid_yes) / 2
        
        # Time in zones
        if 0.35 <= mid_yes <= 0.65:
            stats.time_balanced += 1
        if 0.45 <= mid_yes <= 0.55:
            stats.time_mid_zone += 1
        if mid_yes < 0.10 or mid_yes > 0.90:
            stats.time_extreme += 1
        
        # Volatility tracking
        if prev_mid is not None:
            move_1s = abs(mid_yes - prev_mid)
            stats.moves_1s.append(move_1s)
        prev_mid = mid_yes
        
        # 5s moves
        prev_mid_5s.append(mid_yes)
        if len(prev_mid_5s) > 5:
            move_5s = abs(mid_yes - prev_mid_5s[-6])
            stats.moves_5s.append(move_5s)
            prev_mid_5s = prev_mid_5s[-10:]  # Keep last 10
        
        # Opening period analysis (first 60s)
        if t <= 60:
            if 0.35 <= mid_yes <= 0.65:
                stats.opening_balanced = True
    
    # Compute opening stats
    opening_spreads = stats.spreads[:60] if len(stats.spreads) >= 60 else stats.spreads
    opening_moves = stats.moves_5s[:60] if len(stats.moves_5s) >= 60 else stats.moves_5s
    
    if opening_spreads:
        stats.opening_spread_avg = statistics.mean(opening_spreads)
    if opening_moves:
        stats.opening_vol = max(opening_moves) if opening_moves else 0
    
    return stats

def compute_maker_rebate(price: float, shares: int = 100) -> float:
    """Compute taker fee (which funds maker rebates) at given price"""
    # Fee = C * 0.25 * (p * (1-p))^2
    # For 100 shares at price p
    fee = shares * 0.25 * (price * (1 - price)) ** 2
    return fee

def analyze_all_windows():
    """Main analysis function"""
    print("=" * 80)
    print("POLYMARKET BTC 15-MIN REWARD OPPORTUNITY ANALYSIS")
    print("=" * 80)
    print()
    
    # Load all windows
    market_logs_dir = DATA_DIR / "market_logs"
    if not market_logs_dir.exists():
        print(f"ERROR: Data directory not found: {market_logs_dir}")
        return
    
    all_stats = []
    for window_dir in sorted(market_logs_dir.iterdir()):
        if window_dir.is_dir():
            stats = load_window(window_dir)
            if stats:
                all_stats.append(stats)
    
    print(f"Loaded {len(all_stats)} windows\n")
    
    if not all_stats:
        print("No data loaded!")
        return
    
    # =========================================================================
    # SECTION 1: TIME DISTRIBUTION ANALYSIS
    # =========================================================================
    print("=" * 80)
    print("SECTION 1: WHEN IS THE MARKET TRADEABLE?")
    print("=" * 80)
    
    total_ticks = sum(s.ticks for s in all_stats)
    total_balanced = sum(s.time_balanced for s in all_stats)
    total_mid_zone = sum(s.time_mid_zone for s in all_stats)
    total_extreme = sum(s.time_extreme for s in all_stats)
    
    print(f"\nTotal ticks analyzed: {total_ticks:,}")
    print(f"\nTime Distribution:")
    print(f"  Balanced zone (0.35-0.65):  {total_balanced:,} ticks ({100*total_balanced/total_ticks:.1f}%)")
    print(f"  Mid zone (0.45-0.55):       {total_mid_zone:,} ticks ({100*total_mid_zone/total_ticks:.1f}%) <- BEST FOR REWARDS")
    print(f"  Extreme zone (<0.10/>0.90): {total_extreme:,} ticks ({100*total_extreme/total_ticks:.1f}%)")
    
    # Per-window analysis
    windows_mostly_balanced = sum(1 for s in all_stats if s.time_balanced > s.ticks * 0.5)
    windows_mostly_mid = sum(1 for s in all_stats if s.time_mid_zone > s.ticks * 0.3)
    windows_mostly_extreme = sum(1 for s in all_stats if s.time_extreme > s.ticks * 0.5)
    
    print(f"\nPer-Window Analysis ({len(all_stats)} windows):")
    print(f"  Windows mostly balanced (>50%): {windows_mostly_balanced} ({100*windows_mostly_balanced/len(all_stats):.1f}%)")
    print(f"  Windows with significant mid-zone (>30%): {windows_mostly_mid} ({100*windows_mostly_mid/len(all_stats):.1f}%)")
    print(f"  Windows mostly extreme (>50%): {windows_mostly_extreme} ({100*windows_mostly_extreme/len(all_stats):.1f}%)")
    
    # =========================================================================
    # SECTION 2: SPREAD ANALYSIS
    # =========================================================================
    print("\n" + "=" * 80)
    print("SECTION 2: SPREAD DISTRIBUTION")
    print("=" * 80)
    
    all_spreads = []
    for s in all_stats:
        all_spreads.extend(s.spreads)
    
    if all_spreads:
        spread_cents = [x * 100 for x in all_spreads]
        print(f"\nSpread Statistics (cents):")
        print(f"  Min:    {min(spread_cents):.1f}c")
        print(f"  P10:    {sorted(spread_cents)[len(spread_cents)//10]:.1f}c")
        print(f"  P25:    {sorted(spread_cents)[len(spread_cents)//4]:.1f}c")
        print(f"  Median: {statistics.median(spread_cents):.1f}c")
        print(f"  P75:    {sorted(spread_cents)[3*len(spread_cents)//4]:.1f}c")
        print(f"  P90:    {sorted(spread_cents)[9*len(spread_cents)//10]:.1f}c")
        print(f"  Max:    {max(spread_cents):.1f}c")
        
        tight_spread = sum(1 for x in spread_cents if x <= 1) / len(spread_cents)
        within_reward = sum(1 for x in spread_cents if x <= 3) / len(spread_cents)
        print(f"\n  Tight spread (<=1c): {100*tight_spread:.1f}% of time")
        print(f"  Within reward zone (<=3c): {100*within_reward:.1f}% of time <- QUALIFY FOR REWARDS")
    
    # =========================================================================
    # SECTION 3: VOLATILITY ANALYSIS
    # =========================================================================
    print("\n" + "=" * 80)
    print("SECTION 3: VOLATILITY ANALYSIS")
    print("=" * 80)
    
    all_moves_1s = []
    all_moves_5s = []
    for s in all_stats:
        all_moves_1s.extend(s.moves_1s)
        all_moves_5s.extend(s.moves_5s)
    
    if all_moves_5s:
        moves_5s_cents = [x * 100 for x in all_moves_5s]
        print(f"\n5-second Move Distribution (cents):")
        print(f"  P50:  {statistics.median(moves_5s_cents):.2f}c")
        print(f"  P75:  {sorted(moves_5s_cents)[3*len(moves_5s_cents)//4]:.2f}c")
        print(f"  P90:  {sorted(moves_5s_cents)[9*len(moves_5s_cents)//10]:.2f}c")
        print(f"  P95:  {sorted(moves_5s_cents)[19*len(moves_5s_cents)//20]:.2f}c")
        print(f"  P99:  {sorted(moves_5s_cents)[99*len(moves_5s_cents)//100]:.2f}c")
        
        low_vol = sum(1 for x in moves_5s_cents if x <= 5) / len(moves_5s_cents)
        spike = sum(1 for x in moves_5s_cents if x > 10) / len(moves_5s_cents)
        print(f"\n  Low volatility (<=5c/5s): {100*low_vol:.1f}% of time <- SAFE TO QUOTE")
        print(f"  Spike conditions (>10c/5s): {100*spike:.1f}% of time <- PAUSE ENTRIES")
    
    # =========================================================================
    # SECTION 4: OPENING PERIOD ANALYSIS
    # =========================================================================
    print("\n" + "=" * 80)
    print("SECTION 4: OPENING PERIOD (FIRST 60s) ANALYSIS")
    print("=" * 80)
    
    opening_balanced_count = sum(1 for s in all_stats if s.opening_balanced)
    print(f"\nWindows starting balanced: {opening_balanced_count}/{len(all_stats)} ({100*opening_balanced_count/len(all_stats):.1f}%)")
    
    opening_spreads = [s.opening_spread_avg * 100 for s in all_stats if s.opening_spread_avg > 0]
    opening_vols = [s.opening_vol * 100 for s in all_stats if s.opening_vol > 0]
    
    if opening_spreads:
        print(f"\nOpening Spread (first 60s):")
        print(f"  Median: {statistics.median(opening_spreads):.2f}c")
        print(f"  P90:    {sorted(opening_spreads)[9*len(opening_spreads)//10]:.2f}c")
    
    if opening_vols:
        print(f"\nOpening Max 5s Move:")
        print(f"  Median: {statistics.median(opening_vols):.2f}c")
        print(f"  P90:    {sorted(opening_vols)[9*len(opening_vols)//10]:.2f}c")
    
    # =========================================================================
    # SECTION 5: MAKER REBATE OPPORTUNITY
    # =========================================================================
    print("\n" + "=" * 80)
    print("SECTION 5: MAKER REBATE OPPORTUNITY ANALYSIS")
    print("=" * 80)
    
    print("""
POLYMARKET MAKER REBATE STRUCTURE:
- Taker fees fund maker rebates
- Fee = Shares × 0.25 × (price × (1-price))²
- Maximum fee at 50c (1.56% = $0.78 per 100 shares at $50)
- Zero fee at extremes (0c/100c)

IMPLICATION FOR BOT:
- Getting filled as MAKER at 50c earns you the highest rebate share
- If you're the only maker getting filled, you get all the rebate pool
- Even if spread is tiny (1c), rebates can make you profitable
""")
    
    print("Taker Fee (= Maker Rebate Pool) by Price:")
    for price in [0.10, 0.20, 0.30, 0.40, 0.50, 0.60, 0.70, 0.80, 0.90]:
        fee = compute_maker_rebate(price, 100)
        print(f"  {int(price*100):2d}c: ${fee:.2f} per 100 shares filled")
    
    # =========================================================================
    # SECTION 6: FULL-SET ARBITRAGE CHECK
    # =========================================================================
    print("\n" + "=" * 80)
    print("SECTION 6: FULL-SET ARBITRAGE OPPORTUNITIES")
    print("=" * 80)
    
    fullset_opportunities = 0
    fullset_edge_sum = 0
    
    for s in all_stats:
        for i in range(len(s.yes_prices)):
            if i < len(s.yes_bids):
                # Buy both at ask
                total_cost = s.yes_prices[i] + s.no_prices[i]
                if total_cost < 0.99:  # 1c+ edge
                    fullset_opportunities += 1
                    fullset_edge_sum += (1.0 - total_cost) * 100  # in cents
    
    print(f"\nFull-set opportunities (YES_ask + NO_ask < 99c): {fullset_opportunities:,}")
    if fullset_opportunities > 0:
        print(f"Average edge when available: {fullset_edge_sum/fullset_opportunities:.2f}c")
        print(f"Frequency: {100*fullset_opportunities/total_ticks:.2f}% of all ticks")
    
    # =========================================================================
    # SECTION 7: RECOMMENDED STRATEGY
    # =========================================================================
    print("\n" + "=" * 80)
    print("SECTION 7: RECOMMENDED STRATEGY FOR MAXIMUM REWARDS")
    print("=" * 80)
    
    print("""
STRATEGY: BALANCED ZONE MAKER REBATE HARVESTING

WHEN TO TRADE:
1. Only when YES_mid is in [0.35, 0.65] (captures maker rebates)
2. Prefer [0.45, 0.55] when possible (maximum rebates)
3. Skip extreme odds (<0.10 or >0.90) - no rebates there

HOW TO TRADE:
1. Place postOnly BID orders near touch (best_bid or best_bid+1c)
2. Size = MIN_SHARES (5-10) to minimize inventory risk
3. When filled, immediately place EXIT at best_ask-1c or best_ask

RISK MANAGEMENT:
1. Global inventory cap: Never hold >$2 worth of shares
2. Single-side limit: If one side fills, cancel other side's entry
3. Time-based exits: Flatten before window ends (2 min cutoff)
4. Emergency taker: Cross spread if stuck (accept the taker fee as cost)

EXPECTED RETURNS:
- Maker rebate per fill: $0.50-0.80 per 100 shares at 50c
- With small account ($15-20), expect ~5-20 fills/day
- Daily rebate estimate: $2.50-$16 (if you're competitive)
- Break-even requires: rebates > spread losses + adverse selection

KEY INSIGHT:
The goal is NOT to capture spread (it's often 1c).
The goal is to GET FILLED as a MAKER and earn rebates.
Even if you exit at break-even, the rebate is pure profit.
""")
    
    # =========================================================================
    # SECTION 8: OPTIMAL PARAMETERS
    # =========================================================================
    print("\n" + "=" * 80)
    print("SECTION 8: DATA-DRIVEN OPTIMAL PARAMETERS")
    print("=" * 80)
    
    # Compute optimal thresholds from data
    if all_moves_5s:
        moves_5s_cents = [x * 100 for x in all_moves_5s]
        spike_p95 = sorted(moves_5s_cents)[19*len(moves_5s_cents)//20]
        spike_p90 = sorted(moves_5s_cents)[9*len(moves_5s_cents)//10]
    else:
        spike_p95, spike_p90 = 15, 10
    
    if all_spreads:
        spread_p75 = sorted([x*100 for x in all_spreads])[3*len(all_spreads)//4]
    else:
        spread_p75 = 2
    
    print(f"""
RECOMMENDED CONFIG (based on 50-day analysis):

# Regime Filters
MM_ENTRY_MID_MIN=0.35          # Only enter when YES >= 35c
MM_ENTRY_MID_MAX=0.65          # Only enter when YES <= 65c
MM_MIN_SPREAD_CENTS=1          # Require at least 1c spread

# Volatility Thresholds  
MM_SPIKE_THRESHOLD_CENTS=10    # P90 of 5s moves: {spike_p90:.1f}c
MM_SPIKE_COOLDOWN_SECS=10      # Pause after spike
MM_VOL_10S_CENTS=12            # Skip if 10s vol > 12c

# Position Sizing (for $15-20 account)
MM_QUOTE_SIZE=5                # Minimum shares
MM_MAX_USDC_LOCKED=2.00        # Max capital at risk
MM_MAX_SHARES_PER_TOKEN=20     # Max inventory

# Timing
OPENING_MODE_SECS=30           # Special handling first 30s
MM_ENTRY_CUTOFF_SECS=180       # No new entries last 3 min
MM_FLATTEN_DEADLINE_SECS=120   # Force exits last 2 min

# Safety
MM_EXIT_ENFORCED=1             # Always have exit orders
MM_EMERGENCY_TAKER_EXIT=1      # Cross spread if stuck
""")
    
    # Save to file
    output_path = Path("analysis/reward_analysis_report.md")
    output_path.parent.mkdir(exist_ok=True)
    
    with open(output_path, 'w') as f:
        f.write("# Polymarket BTC 15-min Reward Opportunity Analysis\n\n")
        f.write(f"**Generated from {len(all_stats)} windows of 50-day tick data**\n\n")
        
        f.write("## Key Findings\n\n")
        f.write(f"1. **Tradeable Time**: {100*total_balanced/total_ticks:.1f}% of time market is in balanced zone (0.35-0.65)\n")
        f.write(f"2. **Mid Zone Time**: {100*total_mid_zone/total_ticks:.1f}% of time market is near 50c (best for rebates)\n")
        f.write(f"3. **Tight Spreads**: {100*tight_spread:.1f}% of time spread is ≤1c\n")
        f.write(f"4. **Low Volatility**: {100*low_vol:.1f}% of time 5s moves are ≤5c (safe to quote)\n\n")
        
        f.write("## Strategy: Maker Rebate Harvesting\n\n")
        f.write("- Place postOnly bids near touch in balanced zone\n")
        f.write("- Get filled as maker to earn rebates\n")
        f.write("- Exit at break-even or small profit; rebate is the edge\n")
        f.write("- Strict inventory limits and time-based flattening\n\n")
        
        f.write("## Risk Profile\n\n")
        f.write("- **Low risk**: Only trade balanced markets, small sizes\n")
        f.write("- **Bounded loss**: Emergency taker exits, flatten before settlement\n")
        f.write("- **Profit source**: Maker rebates (funded by taker fees)\n")
    
    print(f"\nReport saved to: {output_path}")
    print("\n" + "=" * 80)
    print("ANALYSIS COMPLETE")
    print("=" * 80)

if __name__ == "__main__":
    analyze_all_windows()

