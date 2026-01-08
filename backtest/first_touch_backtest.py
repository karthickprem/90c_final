#!/usr/bin/env python3
"""
FIRST_TOUCH Strategy Backtest

Strategy: Buy at the FIRST tick where UP >= 90c or DOWN >= 90c.
Analyze win rates, EV, and reversal patterns to find optimal filters.

Output:
- Trade-level data with entry metrics
- Win rate and EV by bucket (secs_left, entry_price, reversal patterns)
- Recommended filters to maximize EV
"""

from __future__ import annotations

import argparse
import csv
import json
import sys
from dataclasses import dataclass, field, asdict
from pathlib import Path
from typing import Optional, List, Dict, Tuple, Any

# Import from main backtest module
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
class FirstTouchTrade:
    """A single first-touch trade."""
    window_id: str
    first_touch_side: str  # "UP" or "DOWN"
    entry_price: int  # cents paid (e.g., 90 if UP=90c)
    entry_time: float  # elapsed seconds at entry
    secs_left: float  # 900 - entry_time (time remaining)
    winner: str  # "UP", "DOWN", or "UNCLEAR"
    won: bool  # did first_touch_side win?
    pnl: float  # 1 - entry_price/100 if won, -entry_price/100 if lost
    
    # Persistence metrics
    persist_90_secs: float  # how many seconds does touched side stay >= 90c
    
    # Reversal metrics
    did_opposite_touch_90_after_entry: bool
    opposite_touch_90_time: Optional[float]  # when did opposite first hit 90 (None if never)
    
    # Price action next 60 seconds
    max_opposite_next60: int  # max cents of opposite side in next 60s
    min_self_next60: int  # min cents of our side in next 60s
    
    # Additional context
    max_opposite_ever: int  # max of opposite side ever after entry
    min_self_ever: int  # min of our side ever after entry


@dataclass
class BucketStats:
    """Statistics for a bucket of trades."""
    bucket_name: str
    bucket_value: str
    n_trades: int = 0
    n_wins: int = 0
    n_losses: int = 0
    total_pnl: float = 0.0
    sum_entry_price: float = 0.0
    n_reversals: int = 0  # opposite touched 90 after entry
    
    @property
    def win_rate(self) -> float:
        return self.n_wins / self.n_trades if self.n_trades > 0 else 0.0
    
    @property
    def avg_entry_price(self) -> float:
        return self.sum_entry_price / self.n_trades if self.n_trades > 0 else 0.0
    
    @property
    def ev(self) -> float:
        """EV = win_rate * (1 - avg_entry) - (1 - win_rate) * avg_entry
              = win_rate - avg_entry_price"""
        return self.total_pnl / self.n_trades if self.n_trades > 0 else 0.0
    
    @property
    def reversal_rate(self) -> float:
        return self.n_reversals / self.n_trades if self.n_trades > 0 else 0.0
    
    def to_dict(self) -> Dict[str, Any]:
        return {
            'bucket_name': self.bucket_name,
            'bucket_value': self.bucket_value,
            'n_trades': self.n_trades,
            'n_wins': self.n_wins,
            'n_losses': self.n_losses,
            'win_rate': round(self.win_rate, 4),
            'avg_entry_price': round(self.avg_entry_price, 2),
            'ev': round(self.ev, 4),
            'reversal_rate': round(self.reversal_rate, 4),
            'total_pnl': round(self.total_pnl, 2),
        }


# ============================================================================
# Analysis Functions
# ============================================================================

def analyze_first_touch(
    window_id: str,
    ticks: List[Tick],
    touch_threshold: int = 90,
    resolve_min: int = 97
) -> Optional[FirstTouchTrade]:
    """
    Analyze a window for the first-touch strategy.
    Returns None if no touch >= threshold or winner is UNCLEAR.
    """
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
        return None  # No touch >= threshold in this window
    
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
    
    # Entry details
    entry_price = entry_tick.up_cents if first_touch_side == "UP" else entry_tick.down_cents
    entry_time = entry_tick.elapsed_seconds
    secs_left = 900.0 - entry_time  # Assuming 15-min window
    
    # Did we win?
    won = (first_touch_side == winner)
    
    # PnL: pay entry_price/100, receive 1 if win, 0 if lose
    if won:
        pnl = 1.0 - entry_price / 100.0
    else:
        pnl = -entry_price / 100.0
    
    # Get ticks after entry
    post_entry_ticks = valid_ticks[entry_idx:]
    
    # Persistence: how long does our side stay >= threshold
    persist_90_secs = 0.0
    for tick in post_entry_ticks:
        our_cents = tick.up_cents if first_touch_side == "UP" else tick.down_cents
        if our_cents >= touch_threshold:
            persist_90_secs = tick.elapsed_seconds - entry_time
        else:
            break  # First drop below threshold ends persistence
    
    # Did opposite touch >= 90 after entry?
    did_opposite_touch_90 = False
    opposite_touch_90_time: Optional[float] = None
    
    for tick in post_entry_ticks[1:]:  # Skip entry tick
        opp_cents = tick.down_cents if first_touch_side == "UP" else tick.up_cents
        if opp_cents >= touch_threshold:
            did_opposite_touch_90 = True
            opposite_touch_90_time = tick.elapsed_seconds
            break
    
    # Price action in next 60 seconds
    max_opposite_next60 = 0
    min_self_next60 = 100
    
    for tick in post_entry_ticks:
        if tick.elapsed_seconds > entry_time + 60:
            break
        opp_cents = tick.down_cents if first_touch_side == "UP" else tick.up_cents
        self_cents = tick.up_cents if first_touch_side == "UP" else tick.down_cents
        max_opposite_next60 = max(max_opposite_next60, opp_cents)
        min_self_next60 = min(min_self_next60, self_cents)
    
    # Max opposite and min self ever after entry
    max_opposite_ever = 0
    min_self_ever = 100
    
    for tick in post_entry_ticks:
        opp_cents = tick.down_cents if first_touch_side == "UP" else tick.up_cents
        self_cents = tick.up_cents if first_touch_side == "UP" else tick.down_cents
        max_opposite_ever = max(max_opposite_ever, opp_cents)
        min_self_ever = min(min_self_ever, self_cents)
    
    return FirstTouchTrade(
        window_id=window_id,
        first_touch_side=first_touch_side,
        entry_price=entry_price,
        entry_time=entry_time,
        secs_left=secs_left,
        winner=winner,
        won=won,
        pnl=pnl,
        persist_90_secs=persist_90_secs,
        did_opposite_touch_90_after_entry=did_opposite_touch_90,
        opposite_touch_90_time=opposite_touch_90_time,
        max_opposite_next60=max_opposite_next60,
        min_self_next60=min_self_next60,
        max_opposite_ever=max_opposite_ever,
        min_self_ever=min_self_ever,
    )


def bucket_secs_left(secs: float) -> str:
    """Bucket seconds left into categories."""
    if secs >= 800:
        return "800+"
    elif secs >= 700:
        return "700-800"
    elif secs >= 600:
        return "600-700"
    elif secs >= 500:
        return "500-600"
    elif secs >= 400:
        return "400-500"
    elif secs >= 300:
        return "300-400"
    elif secs >= 200:
        return "200-300"
    elif secs >= 100:
        return "100-200"
    else:
        return "0-100"


def bucket_entry_price(price: int) -> str:
    """Bucket entry price into categories."""
    if price >= 97:
        return "97-100"
    elif price >= 95:
        return "95-96"
    elif price >= 93:
        return "93-94"
    elif price >= 91:
        return "91-92"
    else:
        return "90"


def compute_bucket_stats(trades: List[FirstTouchTrade]) -> Dict[str, List[BucketStats]]:
    """Compute statistics bucketed by various dimensions."""
    
    buckets: Dict[str, Dict[str, BucketStats]] = {
        'overall': {},
        'by_secs_left': {},
        'by_entry_price': {},
        'by_reversal': {},
        'by_side': {},
        'by_secs_left_and_reversal': {},
        'by_entry_price_and_reversal': {},
    }
    
    # Initialize overall bucket
    buckets['overall']['ALL'] = BucketStats('overall', 'ALL')
    
    for trade in trades:
        # Overall
        b = buckets['overall']['ALL']
        update_bucket(b, trade)
        
        # By secs_left
        secs_bucket = bucket_secs_left(trade.secs_left)
        if secs_bucket not in buckets['by_secs_left']:
            buckets['by_secs_left'][secs_bucket] = BucketStats('secs_left', secs_bucket)
        update_bucket(buckets['by_secs_left'][secs_bucket], trade)
        
        # By entry_price
        price_bucket = bucket_entry_price(trade.entry_price)
        if price_bucket not in buckets['by_entry_price']:
            buckets['by_entry_price'][price_bucket] = BucketStats('entry_price', price_bucket)
        update_bucket(buckets['by_entry_price'][price_bucket], trade)
        
        # By reversal (did opposite touch 90 after entry)
        rev_bucket = "REVERSED" if trade.did_opposite_touch_90_after_entry else "NO_REVERSAL"
        if rev_bucket not in buckets['by_reversal']:
            buckets['by_reversal'][rev_bucket] = BucketStats('reversal', rev_bucket)
        update_bucket(buckets['by_reversal'][rev_bucket], trade)
        
        # By side
        if trade.first_touch_side not in buckets['by_side']:
            buckets['by_side'][trade.first_touch_side] = BucketStats('side', trade.first_touch_side)
        update_bucket(buckets['by_side'][trade.first_touch_side], trade)
        
        # By secs_left AND reversal (combined)
        combo_key = f"{secs_bucket}|{rev_bucket}"
        if combo_key not in buckets['by_secs_left_and_reversal']:
            buckets['by_secs_left_and_reversal'][combo_key] = BucketStats('secs_left+reversal', combo_key)
        update_bucket(buckets['by_secs_left_and_reversal'][combo_key], trade)
        
        # By entry_price AND reversal (combined)
        combo_key2 = f"{price_bucket}|{rev_bucket}"
        if combo_key2 not in buckets['by_entry_price_and_reversal']:
            buckets['by_entry_price_and_reversal'][combo_key2] = BucketStats('entry_price+reversal', combo_key2)
        update_bucket(buckets['by_entry_price_and_reversal'][combo_key2], trade)
    
    # Convert to sorted lists
    result: Dict[str, List[BucketStats]] = {}
    for name, bucket_dict in buckets.items():
        result[name] = sorted(bucket_dict.values(), key=lambda b: b.bucket_value)
    
    return result


def update_bucket(b: BucketStats, trade: FirstTouchTrade) -> None:
    """Update bucket statistics with a trade."""
    b.n_trades += 1
    if trade.won:
        b.n_wins += 1
    else:
        b.n_losses += 1
    b.total_pnl += trade.pnl
    b.sum_entry_price += trade.entry_price
    if trade.did_opposite_touch_90_after_entry:
        b.n_reversals += 1


def find_best_filters(
    trades: List[FirstTouchTrade],
    min_trades: int = 100
) -> List[Dict[str, Any]]:
    """
    Find the best filter combinations that maximize EV while keeping >= min_trades.
    """
    filters: List[Dict[str, Any]] = []
    
    # Filter 1: Exclude reversals
    no_rev_trades = [t for t in trades if not t.did_opposite_touch_90_after_entry]
    if len(no_rev_trades) >= min_trades:
        stats = compute_single_bucket(no_rev_trades)
        filters.append({
            'filter': 'NO_REVERSAL_ONLY',
            'description': 'Exclude trades where opposite touched 90 after entry',
            **stats
        })
    
    # Filter 2: secs_left > X (try various thresholds)
    for min_secs in [100, 200, 300, 400, 500, 600]:
        filtered = [t for t in trades if t.secs_left >= min_secs]
        if len(filtered) >= min_trades:
            stats = compute_single_bucket(filtered)
            filters.append({
                'filter': f'secs_left >= {min_secs}',
                'description': f'Entry with at least {min_secs}s remaining',
                **stats
            })
    
    # Filter 3: entry_price <= X
    for max_price in [90, 91, 92, 93, 94, 95]:
        filtered = [t for t in trades if t.entry_price <= max_price]
        if len(filtered) >= min_trades:
            stats = compute_single_bucket(filtered)
            filters.append({
                'filter': f'entry_price <= {max_price}',
                'description': f'Entry at {max_price}c or below',
                **stats
            })
    
    # Filter 4: Combined - no reversal AND secs_left > X
    for min_secs in [100, 200, 300, 400, 500]:
        filtered = [t for t in trades 
                   if not t.did_opposite_touch_90_after_entry and t.secs_left >= min_secs]
        if len(filtered) >= min_trades:
            stats = compute_single_bucket(filtered)
            filters.append({
                'filter': f'NO_REVERSAL + secs_left >= {min_secs}',
                'description': f'No reversal and at least {min_secs}s remaining',
                **stats
            })
    
    # Filter 5: Combined - entry_price <= X AND secs_left > Y
    for max_price in [91, 92, 93]:
        for min_secs in [200, 300, 400]:
            filtered = [t for t in trades 
                       if t.entry_price <= max_price and t.secs_left >= min_secs]
            if len(filtered) >= min_trades:
                stats = compute_single_bucket(filtered)
                filters.append({
                    'filter': f'entry_price <= {max_price} + secs_left >= {min_secs}',
                    'description': f'Entry at {max_price}c or below with {min_secs}s+ remaining',
                    **stats
                })
    
    # Sort by EV descending
    filters.sort(key=lambda x: x['ev'], reverse=True)
    
    return filters


def compute_single_bucket(trades: List[FirstTouchTrade]) -> Dict[str, Any]:
    """Compute statistics for a single set of trades."""
    if not trades:
        return {'n_trades': 0, 'win_rate': 0, 'ev': 0, 'avg_entry_price': 0, 'reversal_rate': 0}
    
    n_trades = len(trades)
    n_wins = sum(1 for t in trades if t.won)
    total_pnl = sum(t.pnl for t in trades)
    avg_entry = sum(t.entry_price for t in trades) / n_trades
    n_reversals = sum(1 for t in trades if t.did_opposite_touch_90_after_entry)
    
    return {
        'n_trades': n_trades,
        'n_wins': n_wins,
        'win_rate': round(n_wins / n_trades, 4),
        'ev': round(total_pnl / n_trades, 4),
        'avg_entry_price': round(avg_entry, 2),
        'reversal_rate': round(n_reversals / n_trades, 4),
        'total_pnl': round(total_pnl, 2),
    }


# ============================================================================
# Output Functions
# ============================================================================

def write_trades_csv(trades: List[FirstTouchTrade], outdir: Path) -> None:
    """Write trade-level data to CSV."""
    outdir.mkdir(parents=True, exist_ok=True)
    csv_path = outdir / "first_touch_trades.csv"
    
    with open(csv_path, 'w', newline='', encoding='utf-8') as f:
        writer = csv.writer(f)
        writer.writerow([
            'window_id',
            'first_touch_side',
            'entry_price',
            'entry_time',
            'secs_left',
            'winner',
            'won',
            'pnl',
            'persist_90_secs',
            'did_opposite_touch_90_after_entry',
            'opposite_touch_90_time',
            'max_opposite_next60',
            'min_self_next60',
            'max_opposite_ever',
            'min_self_ever',
        ])
        
        for t in trades:
            writer.writerow([
                t.window_id,
                t.first_touch_side,
                t.entry_price,
                f"{t.entry_time:.3f}",
                f"{t.secs_left:.3f}",
                t.winner,
                1 if t.won else 0,
                f"{t.pnl:.4f}",
                f"{t.persist_90_secs:.3f}",
                1 if t.did_opposite_touch_90_after_entry else 0,
                f"{t.opposite_touch_90_time:.3f}" if t.opposite_touch_90_time else "",
                t.max_opposite_next60,
                t.min_self_next60,
                t.max_opposite_ever,
                t.min_self_ever,
            ])


def write_summary_json(
    trades: List[FirstTouchTrade],
    bucket_stats: Dict[str, List[BucketStats]],
    best_filters: List[Dict[str, Any]],
    outdir: Path
) -> None:
    """Write summary JSON."""
    outdir.mkdir(parents=True, exist_ok=True)
    json_path = outdir / "first_touch_summary.json"
    
    summary = {
        'total_trades': len(trades),
        'overall': bucket_stats['overall'][0].to_dict() if bucket_stats['overall'] else {},
        'by_secs_left': [b.to_dict() for b in bucket_stats['by_secs_left']],
        'by_entry_price': [b.to_dict() for b in bucket_stats['by_entry_price']],
        'by_reversal': [b.to_dict() for b in bucket_stats['by_reversal']],
        'by_side': [b.to_dict() for b in bucket_stats['by_side']],
        'by_secs_left_and_reversal': [b.to_dict() for b in bucket_stats['by_secs_left_and_reversal']],
        'by_entry_price_and_reversal': [b.to_dict() for b in bucket_stats['by_entry_price_and_reversal']],
        'best_filters': best_filters[:20],  # Top 20
    }
    
    with open(json_path, 'w', encoding='utf-8') as f:
        json.dump(summary, f, indent=2)


def print_results(
    trades: List[FirstTouchTrade],
    bucket_stats: Dict[str, List[BucketStats]],
    best_filters: List[Dict[str, Any]]
) -> None:
    """Print results to console."""
    
    print("\n" + "=" * 80)
    print("FIRST_TOUCH STRATEGY BACKTEST RESULTS")
    print("=" * 80)
    
    # Overall
    overall = bucket_stats['overall'][0] if bucket_stats['overall'] else None
    if overall:
        print(f"\n--- OVERALL ({overall.n_trades} trades) ---")
        print(f"  Win rate:         {overall.win_rate:.2%}")
        print(f"  Avg entry price:  {overall.avg_entry_price:.1f}c")
        print(f"  EV per trade:     {overall.ev:.4f} ({overall.ev*100:.2f}%)")
        print(f"  Total PnL:        {overall.total_pnl:.2f}")
        print(f"  Reversal rate:    {overall.reversal_rate:.2%}")
    
    # By secs_left
    print(f"\n--- BY SECS_LEFT AT ENTRY ---")
    print(f"{'Secs Left':<12} {'Trades':>7} {'Win%':>8} {'AvgEntry':>9} {'EV':>8} {'Rev%':>8}")
    print("-" * 60)
    for b in sorted(bucket_stats['by_secs_left'], key=lambda x: x.bucket_value, reverse=True):
        print(f"{b.bucket_value:<12} {b.n_trades:>7} {b.win_rate:>7.1%} {b.avg_entry_price:>8.1f}c {b.ev:>8.4f} {b.reversal_rate:>7.1%}")
    
    # By entry_price
    print(f"\n--- BY ENTRY PRICE ---")
    print(f"{'Entry Price':<12} {'Trades':>7} {'Win%':>8} {'AvgEntry':>9} {'EV':>8} {'Rev%':>8}")
    print("-" * 60)
    for b in sorted(bucket_stats['by_entry_price'], key=lambda x: x.bucket_value):
        print(f"{b.bucket_value:<12} {b.n_trades:>7} {b.win_rate:>7.1%} {b.avg_entry_price:>8.1f}c {b.ev:>8.4f} {b.reversal_rate:>7.1%}")
    
    # By reversal
    print(f"\n--- BY REVERSAL (did opposite touch 90 after entry) ---")
    print(f"{'Reversal':<15} {'Trades':>7} {'Win%':>8} {'AvgEntry':>9} {'EV':>8}")
    print("-" * 55)
    for b in bucket_stats['by_reversal']:
        print(f"{b.bucket_value:<15} {b.n_trades:>7} {b.win_rate:>7.1%} {b.avg_entry_price:>8.1f}c {b.ev:>8.4f}")
    
    # By side
    print(f"\n--- BY FIRST TOUCH SIDE ---")
    print(f"{'Side':<12} {'Trades':>7} {'Win%':>8} {'AvgEntry':>9} {'EV':>8} {'Rev%':>8}")
    print("-" * 60)
    for b in bucket_stats['by_side']:
        print(f"{b.bucket_value:<12} {b.n_trades:>7} {b.win_rate:>7.1%} {b.avg_entry_price:>8.1f}c {b.ev:>8.4f} {b.reversal_rate:>7.1%}")
    
    # Secs_left + Reversal combined
    print(f"\n--- BY SECS_LEFT + REVERSAL (KEY TABLE) ---")
    print(f"{'Secs Left':<12} {'Reversal':<15} {'Trades':>7} {'Win%':>8} {'AvgEntry':>9} {'EV':>8}")
    print("-" * 70)
    for b in sorted(bucket_stats['by_secs_left_and_reversal'], 
                    key=lambda x: (x.bucket_value.split('|')[0], x.bucket_value.split('|')[1]), reverse=True):
        parts = b.bucket_value.split('|')
        print(f"{parts[0]:<12} {parts[1]:<15} {b.n_trades:>7} {b.win_rate:>7.1%} {b.avg_entry_price:>8.1f}c {b.ev:>8.4f}")
    
    # Best filters
    print(f"\n--- BEST FILTERS (by EV, min 100 trades) ---")
    print(f"{'Rank':<5} {'Filter':<45} {'Trades':>7} {'Win%':>8} {'EV':>8}")
    print("-" * 80)
    for i, f in enumerate(best_filters[:15], 1):
        print(f"{i:<5} {f['filter']:<45} {f['n_trades']:>7} {f['win_rate']:>7.1%} {f['ev']:>8.4f}")
    
    print("=" * 80 + "\n")


# ============================================================================
# Main
# ============================================================================

def main() -> int:
    parser = argparse.ArgumentParser(
        description="First-Touch Strategy Backtest",
        formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument(
        '--input', '-i',
        required=True,
        help='Input path: directory (Format A) or file (Format B)'
    )
    parser.add_argument(
        '--outdir', '-o',
        default='out_first_touch',
        help='Output directory (default: out_first_touch)'
    )
    parser.add_argument(
        '--touch',
        type=int,
        default=90,
        help='Touch threshold in cents (default: 90)'
    )
    parser.add_argument(
        '--resolve-min',
        type=int,
        default=97,
        help='Resolve threshold in cents (default: 97)'
    )
    parser.add_argument(
        '--min-trades',
        type=int,
        default=100,
        help='Minimum trades for filter recommendations (default: 100)'
    )
    
    args = parser.parse_args()
    
    input_path = Path(args.input)
    outdir = Path(args.outdir)
    
    if not input_path.exists():
        print(f"ERROR: Input path does not exist: {input_path}", file=sys.stderr)
        return 1
    
    print(f"Loading windows from: {input_path}")
    windows = load_windows(input_path)
    print(f"Loaded {len(windows)} windows")
    
    # Analyze each window
    trades: List[FirstTouchTrade] = []
    for window_id, ticks, errors in windows:
        trade = analyze_first_touch(
            window_id, ticks,
            touch_threshold=args.touch,
            resolve_min=args.resolve_min
        )
        if trade is not None:
            trades.append(trade)
    
    print(f"Generated {len(trades)} first-touch trades")
    
    # Compute bucket statistics
    bucket_stats = compute_bucket_stats(trades)
    
    # Find best filters
    best_filters = find_best_filters(trades, min_trades=args.min_trades)
    
    # Write outputs
    write_trades_csv(trades, outdir)
    write_summary_json(trades, bucket_stats, best_filters, outdir)
    
    print(f"\nOutput written to: {outdir}/")
    print(f"  - first_touch_trades.csv")
    print(f"  - first_touch_summary.json")
    
    # Print results
    print_results(trades, bucket_stats, best_filters)
    
    return 0


if __name__ == '__main__':
    sys.exit(main())

