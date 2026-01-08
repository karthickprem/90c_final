#!/usr/bin/env python3
"""
FIRST_TOUCH Strategy - Advanced Filter Analysis

Grid-search filters that only use info available at/near entry time:
1. Late-only: secs_left <= X
2. Persistence: side stays >= 90 for N seconds after first touch
3. Opposite ceiling: opposite stays <= X for M seconds after touch

Goal: Find filters that reduce reversal rate below ~8% to survive 93c fills.
"""

from __future__ import annotations

import argparse
import csv
import json
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional, List, Dict, Tuple, Any

from backtest_btc15 import (
    Tick,
    load_windows,
    segment_ticks_by_reset,
    select_segment,
)


# ============================================================================
# Data Classes
# ============================================================================

@dataclass
class ExtendedTrade:
    """Extended trade data with filter-relevant metrics."""
    window_id: str
    first_touch_side: str
    entry_price: int
    entry_time: float
    secs_left: float
    winner: str
    won: bool
    pnl: float
    did_reversal: bool
    
    # Persistence metrics (how long does touched side stay >= 90)
    persist_2s: bool  # stayed >= 90 for 2s after first touch
    persist_3s: bool
    persist_5s: bool
    persist_8s: bool
    persist_10s: bool
    
    # Opposite ceiling metrics (max opposite in first M seconds)
    opp_max_5s: int
    opp_max_10s: int
    opp_max_15s: int
    opp_max_20s: int
    
    # Price at delayed entry points
    price_at_2s: Optional[int]  # our side's price 2s after first touch
    price_at_3s: Optional[int]
    price_at_5s: Optional[int]


@dataclass
class FilterResult:
    """Results for a single filter configuration."""
    filter_name: str
    filter_desc: str
    n_trades: int
    n_wins: int
    win_rate: float
    avg_entry: float
    reversal_rate: float
    ev: float
    max_fill_tolerable: float  # q value = max fill where EV >= 0
    
    def to_dict(self) -> Dict[str, Any]:
        return {
            'filter': self.filter_name,
            'description': self.filter_desc,
            'trades': self.n_trades,
            'wins': self.n_wins,
            'win_rate': round(self.win_rate, 4),
            'avg_entry_cents': round(self.avg_entry, 2),
            'reversal_rate': round(self.reversal_rate, 4),
            'ev': round(self.ev, 4),
            'max_fill_tolerable_cents': round(self.max_fill_tolerable * 100, 1),
        }


# ============================================================================
# Analysis Functions
# ============================================================================

def analyze_window_extended(
    window_id: str,
    ticks: List[Tick],
    touch_threshold: int = 90,
    resolve_min: int = 97
) -> Optional[ExtendedTrade]:
    """Analyze window with extended metrics for filter testing."""
    
    if not ticks:
        return None
    
    # Segment by timer resets
    segments, _ = segment_ticks_by_reset(ticks)
    segment_ticks, _, _ = select_segment(segments)
    
    if not segment_ticks:
        return None
    
    # Sort by time and filter valid
    ticks_sorted = sorted(segment_ticks, key=lambda t: t.elapsed_seconds)
    valid_ticks = [t for t in ticks_sorted if t.is_valid()]
    
    if not valid_ticks:
        return None
    
    # Find first touch >= threshold
    entry_tick: Optional[Tick] = None
    entry_idx: int = -1
    first_touch_side: str = ""
    
    for i, tick in enumerate(valid_ticks):
        if tick.up_cents >= touch_threshold:
            entry_tick = tick
            entry_idx = i
            first_touch_side = "UP"
            break
        if tick.down_cents >= touch_threshold:
            entry_tick = tick
            entry_idx = i
            first_touch_side = "DOWN"
            break
    
    if entry_tick is None:
        return None
    
    # Determine winner
    resolved_tick: Optional[Tick] = None
    for tick in reversed(valid_ticks):
        if tick.is_resolved(resolve_min):
            resolved_tick = tick
            break
    
    if resolved_tick is not None:
        winner = "UP" if resolved_tick.up_cents > resolved_tick.down_cents else "DOWN"
    else:
        last_tick = valid_ticks[-1]
        price_diff = abs(last_tick.up - last_tick.down)
        max_price = max(last_tick.up, last_tick.down)
        if price_diff < 0.05 and max_price < 0.60:
            return None  # UNCLEAR
        winner = "UP" if last_tick.up_cents > last_tick.down_cents else "DOWN"
    
    entry_price = entry_tick.up_cents if first_touch_side == "UP" else entry_tick.down_cents
    entry_time = entry_tick.elapsed_seconds
    secs_left = 900.0 - entry_time
    won = (first_touch_side == winner)
    pnl = (1.0 - entry_price / 100.0) if won else (-entry_price / 100.0)
    
    # Get post-entry ticks
    post_entry_ticks = valid_ticks[entry_idx:]
    
    # Check for reversal
    did_reversal = False
    for tick in post_entry_ticks[1:]:
        opp_cents = tick.down_cents if first_touch_side == "UP" else tick.up_cents
        if opp_cents >= touch_threshold:
            did_reversal = True
            break
    
    # Persistence checks: did our side stay >= 90 for N seconds?
    def check_persistence(n_seconds: float) -> bool:
        target_time = entry_time + n_seconds
        for tick in post_entry_ticks:
            if tick.elapsed_seconds > target_time:
                return True  # Made it past N seconds while >= 90
            our_cents = tick.up_cents if first_touch_side == "UP" else tick.down_cents
            if our_cents < touch_threshold:
                return False  # Dropped below 90 before N seconds
        return True  # Window ended while still >= 90
    
    persist_2s = check_persistence(2.0)
    persist_3s = check_persistence(3.0)
    persist_5s = check_persistence(5.0)
    persist_8s = check_persistence(8.0)
    persist_10s = check_persistence(10.0)
    
    # Opposite ceiling: max opposite price in first M seconds
    def get_opp_max(m_seconds: float) -> int:
        max_opp = 0
        for tick in post_entry_ticks:
            if tick.elapsed_seconds > entry_time + m_seconds:
                break
            opp_cents = tick.down_cents if first_touch_side == "UP" else tick.up_cents
            max_opp = max(max_opp, opp_cents)
        return max_opp
    
    opp_max_5s = get_opp_max(5.0)
    opp_max_10s = get_opp_max(10.0)
    opp_max_15s = get_opp_max(15.0)
    opp_max_20s = get_opp_max(20.0)
    
    # Price at delayed entry points
    def get_price_at_delay(delay_seconds: float) -> Optional[int]:
        target_time = entry_time + delay_seconds
        best_tick = None
        for tick in post_entry_ticks:
            if tick.elapsed_seconds >= target_time:
                best_tick = tick
                break
        if best_tick:
            return best_tick.up_cents if first_touch_side == "UP" else best_tick.down_cents
        return None
    
    price_at_2s = get_price_at_delay(2.0)
    price_at_3s = get_price_at_delay(3.0)
    price_at_5s = get_price_at_delay(5.0)
    
    return ExtendedTrade(
        window_id=window_id,
        first_touch_side=first_touch_side,
        entry_price=entry_price,
        entry_time=entry_time,
        secs_left=secs_left,
        winner=winner,
        won=won,
        pnl=pnl,
        did_reversal=did_reversal,
        persist_2s=persist_2s,
        persist_3s=persist_3s,
        persist_5s=persist_5s,
        persist_8s=persist_8s,
        persist_10s=persist_10s,
        opp_max_5s=opp_max_5s,
        opp_max_10s=opp_max_10s,
        opp_max_15s=opp_max_15s,
        opp_max_20s=opp_max_20s,
        price_at_2s=price_at_2s,
        price_at_3s=price_at_3s,
        price_at_5s=price_at_5s,
    )


def compute_filter_stats(trades: List[ExtendedTrade], name: str, desc: str) -> FilterResult:
    """Compute statistics for a filtered set of trades."""
    if not trades:
        return FilterResult(name, desc, 0, 0, 0, 0, 0, 0, 0)
    
    n = len(trades)
    wins = sum(1 for t in trades if t.won)
    reversals = sum(1 for t in trades if t.did_reversal)
    total_entry = sum(t.entry_price for t in trades)
    
    win_rate = wins / n
    avg_entry = total_entry / n
    reversal_rate = reversals / n
    ev = win_rate - avg_entry / 100  # EV = q - p
    max_fill = win_rate  # Max fill where q > p is just q itself
    
    return FilterResult(
        filter_name=name,
        filter_desc=desc,
        n_trades=n,
        n_wins=wins,
        win_rate=win_rate,
        avg_entry=avg_entry,
        reversal_rate=reversal_rate,
        ev=ev,
        max_fill_tolerable=max_fill,
    )


def run_filter_grid(trades: List[ExtendedTrade]) -> List[FilterResult]:
    """Run grid search over all filter combinations."""
    results: List[FilterResult] = []
    
    # Baseline
    results.append(compute_filter_stats(trades, "BASELINE", "All first-touch trades"))
    
    # =========================================================================
    # Filter 1: Late-only (secs_left <= X)
    # =========================================================================
    for max_secs in [120, 100, 80, 60, 40, 30, 20]:
        filtered = [t for t in trades if t.secs_left <= max_secs]
        results.append(compute_filter_stats(
            filtered,
            f"LATE_secs<={max_secs}",
            f"Entry with <= {max_secs}s remaining"
        ))
    
    # =========================================================================
    # Filter 2: Persistence (side stays >= 90 for N seconds)
    # =========================================================================
    for n, attr in [(2, 'persist_2s'), (3, 'persist_3s'), (5, 'persist_5s'), 
                    (8, 'persist_8s'), (10, 'persist_10s')]:
        filtered = [t for t in trades if getattr(t, attr)]
        # Use delayed entry price if available
        results.append(compute_filter_stats(
            filtered,
            f"PERSIST_{n}s",
            f"Side stayed >= 90 for {n}s after first touch"
        ))
    
    # =========================================================================
    # Filter 3: Opposite ceiling (opp <= X for first M seconds)
    # =========================================================================
    for ceiling in [15, 20, 25, 30]:
        for m, attr in [(5, 'opp_max_5s'), (10, 'opp_max_10s'), 
                        (15, 'opp_max_15s'), (20, 'opp_max_20s')]:
            filtered = [t for t in trades if getattr(t, attr) <= ceiling]
            results.append(compute_filter_stats(
                filtered,
                f"OPP<={ceiling}_in_{m}s",
                f"Opposite <= {ceiling}c for first {m}s after touch"
            ))
    
    # =========================================================================
    # Combined filters
    # =========================================================================
    
    # Late + Persistence
    for max_secs in [100, 60, 40]:
        for n, attr in [(3, 'persist_3s'), (5, 'persist_5s')]:
            filtered = [t for t in trades if t.secs_left <= max_secs and getattr(t, attr)]
            results.append(compute_filter_stats(
                filtered,
                f"LATE<={max_secs}+PERSIST_{n}s",
                f"<= {max_secs}s left AND persisted {n}s"
            ))
    
    # Late + Opposite ceiling
    for max_secs in [100, 60, 40]:
        for ceiling in [20, 25]:
            filtered = [t for t in trades if t.secs_left <= max_secs and t.opp_max_10s <= ceiling]
            results.append(compute_filter_stats(
                filtered,
                f"LATE<={max_secs}+OPP<={ceiling}",
                f"<= {max_secs}s left AND opp <= {ceiling}c in 10s"
            ))
    
    # Persistence + Opposite ceiling
    for n, attr in [(3, 'persist_3s'), (5, 'persist_5s')]:
        for ceiling in [20, 25]:
            filtered = [t for t in trades if getattr(t, attr) and t.opp_max_10s <= ceiling]
            results.append(compute_filter_stats(
                filtered,
                f"PERSIST_{n}s+OPP<={ceiling}",
                f"Persisted {n}s AND opp <= {ceiling}c in 10s"
            ))
    
    # Triple combo: Late + Persistence + Opposite ceiling
    for max_secs in [60, 40]:
        for n, attr in [(3, 'persist_3s')]:
            for ceiling in [25]:
                filtered = [t for t in trades 
                           if t.secs_left <= max_secs and getattr(t, attr) and t.opp_max_10s <= ceiling]
                results.append(compute_filter_stats(
                    filtered,
                    f"LATE<={max_secs}+PERSIST_{n}s+OPP<={ceiling}",
                    f"<= {max_secs}s, persisted {n}s, opp <= {ceiling}c"
                ))
    
    return results


def create_secs_persist_matrix(trades: List[ExtendedTrade]) -> List[Dict[str, Any]]:
    """Create secs_left × persistence matrix with reversal% and avg entry."""
    
    secs_buckets = [(0, 30), (30, 60), (60, 100), (100, 200), (200, 400), (400, 900)]
    persist_options = [
        ('any', lambda t: True),
        ('persist_2s', lambda t: t.persist_2s),
        ('persist_3s', lambda t: t.persist_3s),
        ('persist_5s', lambda t: t.persist_5s),
    ]
    
    matrix = []
    
    for secs_min, secs_max in secs_buckets:
        for persist_name, persist_fn in persist_options:
            filtered = [t for t in trades 
                       if secs_min <= t.secs_left < secs_max and persist_fn(t)]
            
            if not filtered:
                continue
            
            n = len(filtered)
            wins = sum(1 for t in filtered if t.won)
            reversals = sum(1 for t in filtered if t.did_reversal)
            avg_entry = sum(t.entry_price for t in filtered) / n
            
            matrix.append({
                'secs_left': f"{secs_min}-{secs_max}",
                'persistence': persist_name,
                'trades': n,
                'win_rate': round(wins / n, 4),
                'reversal_rate': round(reversals / n, 4),
                'avg_entry': round(avg_entry, 2),
                'ev': round(wins / n - avg_entry / 100, 4),
                'max_fill': round(wins / n * 100, 1),
            })
    
    return matrix


# ============================================================================
# Output Functions
# ============================================================================

def print_results(results: List[FilterResult], matrix: List[Dict[str, Any]]) -> None:
    """Print results to console."""
    
    print("\n" + "=" * 100)
    print("FIRST_TOUCH FILTER GRID SEARCH RESULTS")
    print("=" * 100)
    
    # Sort by EV descending, but only show filters with >= 50 trades
    valid_results = [r for r in results if r.n_trades >= 50]
    sorted_results = sorted(valid_results, key=lambda x: x.ev, reverse=True)
    
    print(f"\n{'='*100}")
    print("TOP FILTERS BY EV (min 50 trades)")
    print(f"{'='*100}")
    print(f"{'Filter':<40} {'Trades':>7} {'Win%':>7} {'AvgEnt':>7} {'Rev%':>7} {'EV':>8} {'MaxFill':>8}")
    print("-" * 100)
    
    for r in sorted_results[:30]:
        print(f"{r.filter_name:<40} {r.n_trades:>7} {r.win_rate:>6.1%} {r.avg_entry:>6.1f}c {r.reversal_rate:>6.1%} {r.ev:>7.2%} {r.max_fill_tolerable*100:>7.1f}c")
    
    # Filters with reversal rate < 8%
    print(f"\n{'='*100}")
    print("FILTERS WITH REVERSAL RATE < 8% (can survive 93c fills)")
    print(f"{'='*100}")
    low_rev = [r for r in valid_results if r.reversal_rate < 0.08]
    low_rev_sorted = sorted(low_rev, key=lambda x: x.n_trades, reverse=True)
    
    print(f"{'Filter':<40} {'Trades':>7} {'Win%':>7} {'AvgEnt':>7} {'Rev%':>7} {'EV':>8} {'MaxFill':>8}")
    print("-" * 100)
    for r in low_rev_sorted[:20]:
        print(f"{r.filter_name:<40} {r.n_trades:>7} {r.win_rate:>6.1%} {r.avg_entry:>6.1f}c {r.reversal_rate:>6.1%} {r.ev:>7.2%} {r.max_fill_tolerable*100:>7.1f}c")
    
    # Secs_left × Persistence matrix
    print(f"\n{'='*100}")
    print("SECS_LEFT × PERSISTENCE MATRIX (KEY TABLE)")
    print(f"{'='*100}")
    print(f"{'Secs Left':<12} {'Persist':<12} {'Trades':>7} {'Win%':>7} {'Rev%':>7} {'AvgEnt':>7} {'EV':>8} {'MaxFill':>8}")
    print("-" * 100)
    
    for row in matrix:
        print(f"{row['secs_left']:<12} {row['persistence']:<12} {row['trades']:>7} "
              f"{row['win_rate']:>6.1%} {row['reversal_rate']:>6.1%} {row['avg_entry']:>6.1f}c "
              f"{row['ev']:>7.2%} {row['max_fill']:>7.1f}c")
    
    # Summary
    print(f"\n{'='*100}")
    print("INTERPRETATION")
    print(f"{'='*100}")
    print("""
Key insights:
1. MaxFill = the maximum fill price where EV >= 0 (i.e., win_rate as cents)
2. If your real fills are ~93c, you need reversal_rate < 7.82%
3. If your real fills are ~92c, you need reversal_rate < 8.98%
4. If your real fills are ~91c, you need reversal_rate < 10.97%

Best actionable filters (entry-time only):
- LATE filters: Trade only with few seconds left (lower reversal risk)
- PERSIST filters: Wait for confirmation that side stays >= 90 (avoid 1-tick spikes)
- OPP ceiling: Skip if opposite side jumps quickly (early reversal warning)

Combined filters often have best EV but fewer trades.
""")
    
    print("=" * 100 + "\n")


def write_results(
    results: List[FilterResult],
    matrix: List[Dict[str, Any]],
    trades: List[ExtendedTrade],
    outdir: Path
) -> None:
    """Write results to files."""
    outdir.mkdir(parents=True, exist_ok=True)
    
    # Filter results
    with open(outdir / "filter_results.json", 'w') as f:
        json.dump([r.to_dict() for r in results], f, indent=2)
    
    # Matrix
    with open(outdir / "secs_persist_matrix.json", 'w') as f:
        json.dump(matrix, f, indent=2)
    
    # Extended trades CSV
    with open(outdir / "extended_trades.csv", 'w', newline='') as f:
        writer = csv.writer(f)
        writer.writerow([
            'window_id', 'side', 'entry_price', 'secs_left', 'won', 'reversal',
            'persist_2s', 'persist_3s', 'persist_5s', 
            'opp_max_5s', 'opp_max_10s', 'opp_max_15s'
        ])
        for t in trades:
            writer.writerow([
                t.window_id, t.first_touch_side, t.entry_price, 
                f"{t.secs_left:.1f}", int(t.won), int(t.did_reversal),
                int(t.persist_2s), int(t.persist_3s), int(t.persist_5s),
                t.opp_max_5s, t.opp_max_10s, t.opp_max_15s
            ])


# ============================================================================
# Main
# ============================================================================

def main() -> int:
    parser = argparse.ArgumentParser(description="First-Touch Filter Grid Search")
    parser.add_argument('--input', '-i', required=True, help='Input path')
    parser.add_argument('--outdir', '-o', default='out_filters', help='Output directory')
    parser.add_argument('--touch', type=int, default=90, help='Touch threshold')
    parser.add_argument('--resolve-min', type=int, default=97, help='Resolve threshold')
    
    args = parser.parse_args()
    
    input_path = Path(args.input)
    outdir = Path(args.outdir)
    
    if not input_path.exists():
        print(f"ERROR: Input path does not exist: {input_path}", file=sys.stderr)
        return 1
    
    print(f"Loading windows from: {input_path}")
    windows = load_windows(input_path)
    print(f"Loaded {len(windows)} windows")
    
    # Analyze with extended metrics
    trades: List[ExtendedTrade] = []
    for window_id, ticks, errors in windows:
        trade = analyze_window_extended(window_id, ticks, args.touch, args.resolve_min)
        if trade:
            trades.append(trade)
    
    print(f"Generated {len(trades)} extended trades")
    
    # Run filter grid search
    results = run_filter_grid(trades)
    
    # Create secs × persist matrix
    matrix = create_secs_persist_matrix(trades)
    
    # Write outputs
    write_results(results, matrix, trades, outdir)
    print(f"\nOutput written to: {outdir}/")
    
    # Print results
    print_results(results, matrix)
    
    return 0


if __name__ == '__main__':
    sys.exit(main())

