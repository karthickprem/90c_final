"""
Deep analysis of 90c+ entry viability.
Goal: Find conditions where buying at 90-99c is actually profitable.
"""
import os
import json
from dataclasses import dataclass, field
from typing import List, Dict, Optional, Tuple
from collections import defaultdict

from .parse import parse_tick_file, find_window_ids
from .config import DEFAULT_BUY_DIR, DEFAULT_SELL_DIR


@dataclass
class CrossingEvent:
    """Records when a price first crosses a threshold."""
    window_id: str
    side: str  # "UP" or "DOWN"
    cross_price: int  # First price at/above threshold
    cross_time: float  # Elapsed seconds
    final_price: int  # Settlement price (last known)
    time_remaining: float  # 900 - cross_time
    opposite_price_at_cross: int  # What was the other side at?
    settled_win: bool  # Did it reach 97c+?
    max_price_after: int  # Highest price seen after crossing
    min_price_after: int  # Lowest price seen after crossing (reversal depth)
    reversal_depth: int  # How much did it drop after crossing?


@dataclass
class FullSetOpportunity:
    """When both sides are available at combined cost < 100c."""
    window_id: str
    time: float
    up_ask: int
    down_ask: int
    combined_cost: int
    edge: int  # 100 - combined_cost


@dataclass  
class WindowAnalysis:
    """Complete analysis of a single window."""
    window_id: str
    num_ticks: int
    
    # Settlement
    winner: Optional[str] = None  # "UP" or "DOWN"
    
    # Crossing events (first time each threshold is crossed)
    up_crossings: Dict[int, CrossingEvent] = field(default_factory=dict)  # threshold -> event
    down_crossings: Dict[int, CrossingEvent] = field(default_factory=dict)
    
    # Full-set opportunities
    fullset_opps: List[FullSetOpportunity] = field(default_factory=list)
    best_fullset_edge: int = 0


def analyze_window(window_id: str, buy_dir: str, sell_dir: str) -> Optional[WindowAnalysis]:
    """Analyze a single window for spike entry viability."""
    from .parse import load_window_ticks
    from .stream import merge_tick_streams
    
    buy_ticks, sell_ticks = load_window_ticks(window_id, buy_dir, sell_dir)
    if not buy_ticks:
        return None
    
    merged = merge_tick_streams(buy_ticks, sell_ticks)
    if not merged:
        return None
    
    analysis = WindowAnalysis(window_id=window_id, num_ticks=len(merged))
    
    # Determine winner from final tick
    final = merged[-1]
    if final.up_ask >= 97:
        analysis.winner = "UP"
    elif final.down_ask >= 97:
        analysis.winner = "DOWN"
    
    # Track crossing events for various thresholds
    thresholds = [85, 88, 90, 92, 93, 94, 95, 96, 97, 98, 99]
    
    up_crossed = {}  # threshold -> first crossing event
    down_crossed = {}
    
    for i, tick in enumerate(merged):
        time = tick.elapsed_secs
        time_remaining = 900 - time
        
        # Check for full-set opportunity
        combined = tick.up_ask + tick.down_ask
        if combined < 100:
            opp = FullSetOpportunity(
                window_id=window_id,
                time=time,
                up_ask=tick.up_ask,
                down_ask=tick.down_ask,
                combined_cost=combined,
                edge=100 - combined
            )
            analysis.fullset_opps.append(opp)
            if opp.edge > analysis.best_fullset_edge:
                analysis.best_fullset_edge = opp.edge
        
        # Check UP crossings
        for thresh in thresholds:
            if thresh not in up_crossed and tick.up_ask >= thresh:
                # Find max/min after this point
                remaining = merged[i:]
                max_after = max(t.up_ask for t in remaining)
                min_after = min(t.up_ask for t in remaining)
                
                event = CrossingEvent(
                    window_id=window_id,
                    side="UP",
                    cross_price=tick.up_ask,
                    cross_time=time,
                    final_price=final.up_ask,
                    time_remaining=time_remaining,
                    opposite_price_at_cross=tick.down_ask,
                    settled_win=(final.up_ask >= 97),
                    max_price_after=max_after,
                    min_price_after=min_after,
                    reversal_depth=tick.up_ask - min_after
                )
                up_crossed[thresh] = event
        
        # Check DOWN crossings
        for thresh in thresholds:
            if thresh not in down_crossed and tick.down_ask >= thresh:
                remaining = merged[i:]
                max_after = max(t.down_ask for t in remaining)
                min_after = min(t.down_ask for t in remaining)
                
                event = CrossingEvent(
                    window_id=window_id,
                    side="DOWN",
                    cross_price=tick.down_ask,
                    cross_time=time,
                    final_price=final.down_ask,
                    time_remaining=time_remaining,
                    opposite_price_at_cross=tick.up_ask,
                    settled_win=(final.down_ask >= 97),
                    max_price_after=max_after,
                    min_price_after=min_after,
                    reversal_depth=tick.down_ask - min_after
                )
                down_crossed[thresh] = event
    
    analysis.up_crossings = up_crossed
    analysis.down_crossings = down_crossed
    
    return analysis


def run_full_analysis(buy_dir: str = DEFAULT_BUY_DIR, sell_dir: str = DEFAULT_SELL_DIR):
    """Run analysis on all windows."""
    window_ids = find_window_ids(buy_dir)
    print(f"Analyzing {len(window_ids)} windows...")
    
    all_analyses = []
    
    # Stats collectors
    crossing_stats = defaultdict(lambda: {"wins": 0, "losses": 0, "reversals": []})
    fullset_counts = defaultdict(int)  # edge bucket -> count
    
    for i, wid in enumerate(window_ids):
        if i % 500 == 0:
            print(f"  Processing {i}/{len(window_ids)}")
        
        analysis = analyze_window(wid, buy_dir, sell_dir)
        if analysis is None:
            continue
        
        all_analyses.append(analysis)
        
        # Aggregate crossing stats
        for thresh, event in {**analysis.up_crossings, **analysis.down_crossings}.items():
            key = f"{thresh}c"
            if event.settled_win:
                crossing_stats[key]["wins"] += 1
            else:
                crossing_stats[key]["losses"] += 1
            crossing_stats[key]["reversals"].append(event.reversal_depth)
        
        # Aggregate full-set stats
        if analysis.best_fullset_edge > 0:
            bucket = (analysis.best_fullset_edge // 2) * 2
            fullset_counts[bucket] += 1
    
    return all_analyses, crossing_stats, fullset_counts


def print_crossing_analysis(crossing_stats: dict):
    """Print win rates by entry threshold."""
    print("\n" + "="*70)
    print("WIN RATE BY ENTRY PRICE THRESHOLD")
    print("="*70)
    print(f"{'Threshold':<12} {'Wins':<8} {'Losses':<8} {'Win Rate':<10} {'Avg Reversal':<12} {'EV/Trade'}")
    print("-"*70)
    
    for thresh in sorted(crossing_stats.keys(), key=lambda x: int(x.replace('c',''))):
        stats = crossing_stats[thresh]
        wins = stats["wins"]
        losses = stats["losses"]
        total = wins + losses
        if total == 0:
            continue
        
        win_rate = wins / total
        avg_reversal = sum(stats["reversals"]) / len(stats["reversals"]) if stats["reversals"] else 0
        
        # Calculate EV
        entry_price = int(thresh.replace('c', ''))
        profit_if_win = 100 - entry_price
        loss_if_lose = entry_price
        ev = (win_rate * profit_if_win) - ((1 - win_rate) * loss_if_lose)
        
        ev_color = "+" if ev > 0 else ""
        print(f"{thresh:<12} {wins:<8} {losses:<8} {win_rate*100:>6.1f}%    {avg_reversal:>6.1f}c      {ev_color}{ev:.2f}c")
    
    print()


def print_time_segmented_analysis(all_analyses: list):
    """Analyze win rates by time remaining at entry."""
    print("\n" + "="*70)
    print("WIN RATE BY TIME REMAINING AT 90c CROSSING")
    print("="*70)
    
    # Buckets: 0-60s, 60-180s, 180-300s, 300-600s, 600-900s
    time_buckets = [
        (0, 60, "0-1 min left"),
        (60, 180, "1-3 min left"),
        (180, 300, "3-5 min left"),
        (300, 600, "5-10 min left"),
        (600, 900, "10-15 min left"),
    ]
    
    bucket_stats = {name: {"wins": 0, "losses": 0} for _, _, name in time_buckets}
    
    for analysis in all_analyses:
        # Look at 90c crossings
        for crossings in [analysis.up_crossings, analysis.down_crossings]:
            if 90 in crossings:
                event = crossings[90]
                time_left = event.time_remaining
                
                for low, high, name in time_buckets:
                    if low <= time_left < high:
                        if event.settled_win:
                            bucket_stats[name]["wins"] += 1
                        else:
                            bucket_stats[name]["losses"] += 1
                        break
    
    print(f"{'Time Remaining':<18} {'Wins':<8} {'Losses':<8} {'Win Rate':<10} {'EV at 90c'}")
    print("-"*60)
    
    for _, _, name in time_buckets:
        stats = bucket_stats[name]
        total = stats["wins"] + stats["losses"]
        if total == 0:
            continue
        win_rate = stats["wins"] / total
        ev = (win_rate * 10) - ((1 - win_rate) * 90)
        ev_str = f"+{ev:.2f}c" if ev > 0 else f"{ev:.2f}c"
        print(f"{name:<18} {stats['wins']:<8} {stats['losses']:<8} {win_rate*100:>6.1f}%    {ev_str}")


def print_opposite_side_analysis(all_analyses: list):
    """Analyze full-set opportunity when crossing 90c."""
    print("\n" + "="*70)
    print("FULL-SET OPPORTUNITY AT 90c CROSSING")
    print("="*70)
    print("When UP crosses 90c, what's the DOWN price? Can we complete full-set?")
    print()
    
    opp_price_buckets = defaultdict(lambda: {"count": 0, "wins": 0})
    
    for analysis in all_analyses:
        for crossings in [analysis.up_crossings, analysis.down_crossings]:
            if 90 in crossings:
                event = crossings[90]
                opp = event.opposite_price_at_cross
                bucket = (opp // 5) * 5
                opp_price_buckets[bucket]["count"] += 1
                if event.settled_win:
                    opp_price_buckets[bucket]["wins"] += 1
    
    print(f"{'Opposite Side':<15} {'Count':<8} {'Win Rate':<10} {'Combined Cost':<15} {'Full-Set Edge'}")
    print("-"*65)
    
    for bucket in sorted(opp_price_buckets.keys()):
        stats = opp_price_buckets[bucket]
        if stats["count"] < 10:
            continue
        win_rate = stats["wins"] / stats["count"]
        combined = 90 + bucket
        edge = 100 - combined if combined < 100 else -(combined - 100)
        edge_str = f"+{edge}c" if edge > 0 else f"{edge}c"
        print(f"{bucket}c             {stats['count']:<8} {win_rate*100:>6.1f}%    {combined}c              {edge_str}")


def print_reversal_analysis(all_analyses: list):
    """Analyze reversal patterns to find safe entry conditions."""
    print("\n" + "="*70)
    print("REVERSAL DEPTH ANALYSIS (at 90c crossing)")
    print("="*70)
    print("How deep do reversals go after crossing 90c?")
    print()
    
    reversal_buckets = defaultdict(lambda: {"wins": 0, "losses": 0})
    
    for analysis in all_analyses:
        for crossings in [analysis.up_crossings, analysis.down_crossings]:
            if 90 in crossings:
                event = crossings[90]
                # Bucket reversal depth
                bucket = (event.reversal_depth // 5) * 5
                if event.settled_win:
                    reversal_buckets[bucket]["wins"] += 1
                else:
                    reversal_buckets[bucket]["losses"] += 1
    
    print(f"{'Max Reversal':<15} {'Count':<8} {'Win Rate':<10} {'Implication'}")
    print("-"*60)
    
    for bucket in sorted(reversal_buckets.keys()):
        stats = reversal_buckets[bucket]
        total = stats["wins"] + stats["losses"]
        if total < 5:
            continue
        win_rate = stats["wins"] / total
        
        if bucket == 0:
            impl = "No reversal - price only went up"
        elif bucket <= 5:
            impl = "Minor pullback"
        elif bucket <= 10:
            impl = "Moderate reversal"
        else:
            impl = "DEEP REVERSAL - dangerous!"
        
        print(f"{bucket}c             {total:<8} {win_rate*100:>6.1f}%    {impl}")


def find_profitable_conditions(all_analyses: list):
    """Find specific conditions where 90c+ entries ARE profitable."""
    print("\n" + "="*70)
    print("SEARCHING FOR PROFITABLE ENTRY CONDITIONS")
    print("="*70)
    
    # Condition 1: Late entry (< 2 min remaining) + 93c+ price
    late_high = {"wins": 0, "losses": 0}
    
    # Condition 2: Full-set available (opposite side < 8c)
    fullset_available = {"wins": 0, "losses": 0}
    
    # Condition 3: Price persistence (already at 90c for > 30 seconds)
    # This would need more tracking, skip for now
    
    # Condition 4: Very high entry (96c+) late in window
    very_high_late = {"wins": 0, "losses": 0}
    
    for analysis in all_analyses:
        for crossings in [analysis.up_crossings, analysis.down_crossings]:
            if 93 in crossings:
                event = crossings[93]
                # Condition 1: Late + high
                if event.time_remaining < 120:  # < 2 min left
                    if event.settled_win:
                        late_high["wins"] += 1
                    else:
                        late_high["losses"] += 1
                
                # Condition 2: Full-set available
                if event.opposite_price_at_cross <= 8:
                    if event.settled_win:
                        fullset_available["wins"] += 1
                    else:
                        fullset_available["losses"] += 1
            
            if 96 in crossings:
                event = crossings[96]
                if event.time_remaining < 60:  # < 1 min left
                    if event.settled_win:
                        very_high_late["wins"] += 1
                    else:
                        very_high_late["losses"] += 1
    
    print("\nCondition Analysis:")
    print("-"*60)
    
    conditions = [
        ("93c+ entry, <2 min left", late_high, 93),
        ("93c+ entry, opposite <8c (full-set)", fullset_available, 93),
        ("96c+ entry, <1 min left", very_high_late, 96),
    ]
    
    for name, stats, entry_price in conditions:
        total = stats["wins"] + stats["losses"]
        if total == 0:
            print(f"{name}: No data")
            continue
        win_rate = stats["wins"] / total
        profit_if_win = 100 - entry_price
        ev = (win_rate * profit_if_win) - ((1 - win_rate) * entry_price)
        ev_str = f"+{ev:.2f}c" if ev > 0 else f"{ev:.2f}c"
        profitable = "[PROFITABLE]" if ev > 0 else "[Unprofitable]"
        
        print(f"{name}")
        print(f"  Samples: {total}, Win Rate: {win_rate*100:.1f}%, EV: {ev_str} {profitable}")
        print()


def main():
    """Run complete analysis."""
    print("="*70)
    print("SPIKE ENTRY VIABILITY ANALYSIS")
    print("Analyzing 50 days of BTC 15m data for profitable 90-99c entries")
    print("="*70)
    
    all_analyses, crossing_stats, fullset_counts = run_full_analysis()
    
    print(f"\nAnalyzed {len(all_analyses)} valid windows")
    
    print_crossing_analysis(crossing_stats)
    print_time_segmented_analysis(all_analyses)
    print_opposite_side_analysis(all_analyses)
    print_reversal_analysis(all_analyses)
    find_profitable_conditions(all_analyses)
    
    # Final recommendation
    print("\n" + "="*70)
    print("RECOMMENDATIONS")
    print("="*70)
    print("""
Based on the analysis, here are strategies to make 90-99c entries profitable:

1. FULL-SET HYBRID: When price crosses 90c, check if opposite side is <10c.
   If UP=90c and DOWN=8c, buy BOTH → 98c cost → guaranteed 2c profit.
   This converts directional risk to execution risk.

2. LATE ENTRY ONLY: Only enter when <2 minutes remain in window.
   Higher prices late in window have much higher win rates because
   there's less time for reversal.

3. CONFIRMATION STACKING: Require multiple conditions:
   - Price at 93c+ (not just 90c)
   - Time remaining < 120s
   - Price has been above 88c for at least 30s (momentum persistence)
   - Opposite side < 10c (near-certain outcome)

4. POSITION SIZING: If you must trade at 90c:
   - Risk only what you can afford to lose 90% of
   - Need 90%+ win rate to break even
   - Size down as entry price increases

5. HYBRID FULL-SET + SPIKE: Primary strategy is full-set accumulation.
   Spike entries are opportunistic additions when conditions are ideal.
""")


if __name__ == "__main__":
    main()

