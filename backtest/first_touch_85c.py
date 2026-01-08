#!/usr/bin/env python3
"""
First Touch Backtest at 85c Threshold

Same framework as 90c, but with lower trigger threshold.
Tests if earlier entry with lower fill price compensates for potentially lower win rate.
"""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import List, Dict, Any, Optional, Tuple

from backtest_btc15 import load_windows, Tick, segment_ticks_by_reset


@dataclass
class FirstTouch85Trade:
    """A single first-touch trade at 85c threshold."""
    window_id: str
    first_touch_side: str  # 'UP' or 'DOWN'
    entry_price: int  # cents
    secs_left: float  # seconds remaining at entry
    
    # Winner info
    winner: str  # 'UP', 'DOWN', or 'UNCLEAR'
    won: bool
    
    # Reversal tracking
    did_opposite_touch_85: bool  # opposite touched 85c after entry
    did_opposite_touch_90: bool  # opposite touched 90c after entry
    
    # Persistence flags (at 85c threshold)
    persist_3s: bool = False
    persist_5s: bool = False
    persist_8s: bool = False
    persist_10s: bool = False
    
    # Opposite ceiling in time windows after touch
    opp_max_10s: int = 100  # max opposite price in 10s after touch
    opp_max_15s: int = 100
    opp_max_20s: int = 100
    
    # Self min (how low did our side drop after entry)
    self_min_10s: int = 0
    self_min_20s: int = 0
    
    # Computed
    pnl: float = 0.0  # +1-p if won, -p if lost


def determine_winner(ticks: List[Tick]) -> Tuple[str, Optional[float]]:
    """Determine winner from ticks using resolution logic."""
    if not ticks:
        return 'UNCLEAR', None
    
    valid = [t for t in ticks if 0 <= t.up_cents <= 100 and 0 <= t.down_cents <= 100]
    if not valid:
        return 'UNCLEAR', None
    
    # Sort by elapsed time
    sorted_ticks = sorted(valid, key=lambda t: t.elapsed_seconds)
    
    # Look for resolved state from end
    resolve_time = None
    winner = None
    
    for t in reversed(sorted_ticks):
        up, down = t.up_cents / 100, t.down_cents / 100
        if max(up, down) >= 0.97 and min(up, down) <= 0.03:
            resolve_time = t.elapsed_seconds
            winner = 'UP' if up > down else 'DOWN'
            break
    
    if winner:
        return winner, resolve_time
    
    # Fallback to last valid tick
    last = sorted_ticks[-1]
    up, down = last.up_cents / 100, last.down_cents / 100
    
    if abs(up - down) < 0.05 and max(up, down) < 0.60:
        return 'UNCLEAR', None
    
    return ('UP' if up > down else 'DOWN'), last.elapsed_seconds


def analyze_first_touch_85(
    window_id: str,
    ticks: List[Tick],
    threshold: int = 85
) -> Optional[FirstTouch85Trade]:
    """
    Analyze a window for first-touch at given threshold (default 85c).
    """
    # Segment by timer reset - get first segment only
    segments, num_resets = segment_ticks_by_reset(ticks)
    if not segments or not segments[0]:
        return None
    segmented = segments[0]  # Use first segment only
    
    valid = [t for t in segmented if 0 <= t.up_cents <= 100 and 0 <= t.down_cents <= 100]
    if not valid:
        return None
    
    sorted_ticks = sorted(valid, key=lambda t: t.elapsed_seconds)
    
    # Find first touch of threshold
    first_touch_idx = None
    first_touch_side = None
    
    for i, t in enumerate(sorted_ticks):
        up_touched = t.up_cents >= threshold
        down_touched = t.down_cents >= threshold
        
        if up_touched and down_touched:
            # Tie-break: higher price wins
            first_touch_side = 'UP' if t.up_cents >= t.down_cents else 'DOWN'
            first_touch_idx = i
            break
        elif up_touched:
            first_touch_side = 'UP'
            first_touch_idx = i
            break
        elif down_touched:
            first_touch_side = 'DOWN'
            first_touch_idx = i
            break
    
    if first_touch_idx is None:
        return None  # Never touched threshold
    
    entry_tick = sorted_ticks[first_touch_idx]
    entry_price = entry_tick.up_cents if first_touch_side == 'UP' else entry_tick.down_cents
    entry_time = entry_tick.elapsed_seconds
    
    # Estimate max time (900s for 15-min window)
    max_time = max(t.elapsed_seconds for t in sorted_ticks)
    secs_left = max(0, 900 - entry_time) if max_time < 850 else max(0, max_time - entry_time)
    
    # Determine winner
    winner, resolve_time = determine_winner(sorted_ticks)
    if winner == 'UNCLEAR':
        return None
    
    won = (first_touch_side == winner)
    
    # Post-entry ticks
    post_entry = [t for t in sorted_ticks if t.elapsed_seconds > entry_time]
    
    # Did opposite touch 85c after entry?
    did_opposite_touch_85 = False
    did_opposite_touch_90 = False
    
    for t in post_entry:
        opp_price = t.down_cents if first_touch_side == 'UP' else t.up_cents
        if opp_price >= 85:
            did_opposite_touch_85 = True
        if opp_price >= 90:
            did_opposite_touch_90 = True
    
    # Persistence: did our side stay >= threshold for N seconds?
    def check_persistence(n_secs: float) -> bool:
        end_time = entry_time + n_secs
        for t in sorted_ticks:
            if t.elapsed_seconds > entry_time and t.elapsed_seconds <= end_time:
                self_price = t.up_cents if first_touch_side == 'UP' else t.down_cents
                if self_price < threshold:
                    return False
        return True
    
    persist_3s = check_persistence(3)
    persist_5s = check_persistence(5)
    persist_8s = check_persistence(8)
    persist_10s = check_persistence(10)
    
    # Opposite max in time windows
    def get_opp_max(n_secs: float) -> int:
        end_time = entry_time + n_secs
        opp_prices = []
        for t in sorted_ticks:
            if t.elapsed_seconds > entry_time and t.elapsed_seconds <= end_time:
                opp_price = t.down_cents if first_touch_side == 'UP' else t.up_cents
                opp_prices.append(opp_price)
        return max(opp_prices) if opp_prices else 0
    
    opp_max_10s = get_opp_max(10)
    opp_max_15s = get_opp_max(15)
    opp_max_20s = get_opp_max(20)
    
    # Self min in time windows
    def get_self_min(n_secs: float) -> int:
        end_time = entry_time + n_secs
        self_prices = []
        for t in sorted_ticks:
            if t.elapsed_seconds > entry_time and t.elapsed_seconds <= end_time:
                self_price = t.up_cents if first_touch_side == 'UP' else t.down_cents
                self_prices.append(self_price)
        return min(self_prices) if self_prices else entry_price
    
    self_min_10s = get_self_min(10)
    self_min_20s = get_self_min(20)
    
    # PnL
    p = entry_price / 100
    pnl = (1 - p) if won else (-p)
    
    return FirstTouch85Trade(
        window_id=window_id,
        first_touch_side=first_touch_side,
        entry_price=entry_price,
        secs_left=secs_left,
        winner=winner,
        won=won,
        did_opposite_touch_85=did_opposite_touch_85,
        did_opposite_touch_90=did_opposite_touch_90,
        persist_3s=persist_3s,
        persist_5s=persist_5s,
        persist_8s=persist_8s,
        persist_10s=persist_10s,
        opp_max_10s=opp_max_10s,
        opp_max_15s=opp_max_15s,
        opp_max_20s=opp_max_20s,
        self_min_10s=self_min_10s,
        self_min_20s=self_min_20s,
        pnl=pnl,
    )


def compute_stats(trades: List[FirstTouch85Trade], label: str = "") -> Dict[str, Any]:
    """Compute statistics for a set of trades."""
    if not trades:
        return {'label': label, 'trades': 0}
    
    n = len(trades)
    wins = sum(1 for t in trades if t.won)
    reversals_85 = sum(1 for t in trades if t.did_opposite_touch_85)
    reversals_90 = sum(1 for t in trades if t.did_opposite_touch_90)
    
    total_entry = sum(t.entry_price for t in trades)
    avg_entry = total_entry / n
    
    q = wins / n
    p = avg_entry / 100
    
    ev_per_share = q - p
    ev_return = (q - p) / p if p > 0 else 0
    
    return {
        'label': label,
        'trades': n,
        'wins': wins,
        'win_rate': round(q, 4),
        'avg_entry': round(avg_entry, 2),
        'reversal_85_rate': round(reversals_85 / n, 4),
        'reversal_90_rate': round(reversals_90 / n, 4),
        'ev_per_share': round(ev_per_share, 4),
        'ev_return': round(ev_return, 4),
        'total_pnl': round(sum(t.pnl for t in trades), 2),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="First Touch Backtest at 85c")
    parser.add_argument('--input', '-i', required=True, help='Input path')
    parser.add_argument('--outdir', '-o', default='out_85c', help='Output directory')
    parser.add_argument('--threshold', '-t', type=int, default=85, help='Touch threshold in cents')
    
    args = parser.parse_args()
    input_path = Path(args.input)
    outdir = Path(args.outdir)
    threshold = args.threshold
    
    if not input_path.exists():
        print(f"ERROR: Input does not exist: {input_path}", file=sys.stderr)
        return 1
    
    print(f"Loading windows from: {input_path}")
    print(f"Touch threshold: {threshold}c")
    windows = load_windows(input_path)
    print(f"Loaded {len(windows)} windows")
    
    # Analyze all windows
    trades: List[FirstTouch85Trade] = []
    no_touch_count = 0
    
    for window_id, ticks, _ in windows:
        trade = analyze_first_touch_85(window_id, ticks, threshold)
        if trade:
            trades.append(trade)
        else:
            no_touch_count += 1
    
    print(f"Generated {len(trades)} trades")
    print(f"Windows without {threshold}c touch: {no_touch_count}")
    
    # Entry price distribution
    entry_prices = [t.entry_price for t in trades]
    print(f"\nEntry price distribution:")
    print(f"  Min: {min(entry_prices)}c")
    print(f"  Max: {max(entry_prices)}c")
    print(f"  Mean: {sum(entry_prices)/len(entry_prices):.2f}c")
    
    # Bucket distribution
    buckets = {}
    for ep in entry_prices:
        bucket = (ep // 5) * 5  # 85, 90, 95, etc
        buckets[bucket] = buckets.get(bucket, 0) + 1
    print(f"  Buckets (5c): {dict(sorted(buckets.items()))}")
    
    # Overall stats
    print("\n" + "=" * 100)
    print(f"OVERALL STATS (threshold = {threshold}c)")
    print("=" * 100)
    overall = compute_stats(trades, "ALL")
    print(f"  Trades:        {overall['trades']}")
    print(f"  Win rate:      {overall['win_rate']:.2%}")
    print(f"  Avg entry:     {overall['avg_entry']:.1f}c")
    print(f"  Reversal@85:   {overall['reversal_85_rate']:.2%}")
    print(f"  Reversal@90:   {overall['reversal_90_rate']:.2%}")
    print(f"  EV/share:      {overall['ev_per_share']:+.2%}")
    print(f"  EV/invested:   {overall['ev_return']:+.2%}")
    
    # Filter tests
    print("\n" + "=" * 100)
    print("FILTER COMPARISON")
    print("=" * 100)
    
    filters = {
        'BASELINE': lambda t: True,
        'PERSIST_3s': lambda t: t.persist_3s,
        'PERSIST_5s': lambda t: t.persist_5s,
        'PERSIST_8s': lambda t: t.persist_8s,
        'PERSIST_10s': lambda t: t.persist_10s,
        'OPP<=20_in_10s': lambda t: t.opp_max_10s <= 20,
        'OPP<=25_in_10s': lambda t: t.opp_max_10s <= 25,
        'OPP<=25_in_20s': lambda t: t.opp_max_20s <= 25,
        'OPP<=30_in_20s': lambda t: t.opp_max_20s <= 30,
        'PERSIST_5s+OPP<=25': lambda t: t.persist_5s and t.opp_max_10s <= 25,
        'PERSIST_8s+OPP<=25': lambda t: t.persist_8s and t.opp_max_10s <= 25,
        'PERSIST_10s+OPP<=25': lambda t: t.persist_10s and t.opp_max_10s <= 25,
        'LATE<=60': lambda t: t.secs_left <= 60,
        'LATE<=100': lambda t: t.secs_left <= 100,
        'LATE<=60+PERSIST_5s': lambda t: t.secs_left <= 60 and t.persist_5s,
        'LATE<=100+PERSIST_5s': lambda t: t.secs_left <= 100 and t.persist_5s,
    }
    
    print(f"{'Filter':<30} {'Trades':>7} {'Win%':>8} {'AvgEnt':>8} {'Rev@85':>8} {'Rev@90':>8} {'EV/sh':>10} {'EV/inv':>10}")
    print("-" * 100)
    
    results = {}
    for name, fn in filters.items():
        filtered = [t for t in trades if fn(t)]
        stats = compute_stats(filtered, name)
        results[name] = stats
        
        if stats['trades'] > 0:
            print(f"{name:<30} {stats['trades']:>7} {stats['win_rate']:>7.2%} "
                  f"{stats['avg_entry']:>7.1f}c {stats['reversal_85_rate']:>7.2%} "
                  f"{stats['reversal_90_rate']:>7.2%} {stats['ev_per_share']:>+9.2%} "
                  f"{stats['ev_return']:>+9.2%}")
    
    # Fill cap simulation for best filter
    print("\n" + "=" * 100)
    print("FILL CAP SIMULATION (PERSIST_10s filter)")
    print("=" * 100)
    
    persist10_trades = [t for t in trades if t.persist_10s]
    
    print(f"{'p_max':>7} {'Exec':>7} {'Win%':>8} {'AvgFill':>9} {'Rev@90':>8} {'EV/share':>10} {'EV/inv':>10}")
    print("-" * 80)
    
    for p_max in [85, 86, 87, 88, 89, 90, 91, 92, 93, 94, 95, 100]:
        executed = [t for t in persist10_trades if t.entry_price <= p_max]
        if not executed:
            continue
        
        n = len(executed)
        wins = sum(1 for t in executed if t.won)
        reversals = sum(1 for t in executed if t.did_opposite_touch_90)
        
        avg_fill = sum(t.entry_price for t in executed) / n
        q = wins / n
        p = avg_fill / 100
        
        ev_share = q - p
        ev_inv = (q - p) / p if p > 0 else 0
        
        print(f"{p_max:>6}c {n:>7} {q:>7.2%} {avg_fill:>8.1f}c "
              f"{reversals/n:>7.2%} {ev_share:>+9.2%} {ev_inv:>+9.2%}")
    
    # Comparison with 90c
    print("\n" + "=" * 100)
    print(f"KEY NUMBERS FOR {threshold}c TRIGGER (copy these)")
    print("=" * 100)
    
    # Best filter stats
    best_filter = 'PERSIST_10s'
    best = results.get(best_filter, {})
    
    print(f"\nBest filter: {best_filter}")
    print(f"  trades:    {best.get('trades', 0)}")
    print(f"  win%:      {best.get('win_rate', 0):.2%}")
    print(f"  avg_entry: {best.get('avg_entry', 0):.1f}c")
    print(f"  EV/share:  {best.get('ev_per_share', 0):+.2%}")
    print(f"  Reversal@90: {best.get('reversal_90_rate', 0):.2%}")
    
    print("\nGO/NO-GO CHECK:")
    if best.get('trades', 0) > 0:
        q = best['win_rate']
        p = best['avg_entry'] / 100
        buffer = q - p
        
        print(f"  win% ({q:.2%}) vs avg_entry ({p:.2%})")
        print(f"  Buffer: {buffer:+.2%}")
        
        if buffer > 0.01:
            print(f"  VERDICT: VIABLE (buffer > 1%)")
        elif buffer > 0:
            print(f"  VERDICT: MARGINAL (buffer < 1%, risky with slippage)")
        else:
            print(f"  VERDICT: NOT VIABLE (negative EV)")
    
    # Write results
    outdir.mkdir(parents=True, exist_ok=True)
    with open(outdir / f"first_touch_{threshold}c.json", 'w') as f:
        json.dump({
            'threshold': threshold,
            'overall': overall,
            'filters': results,
        }, f, indent=2)
    
    print(f"\nResults written to: {outdir}/first_touch_{threshold}c.json")
    
    return 0


if __name__ == '__main__':
    sys.exit(main())

