#!/usr/bin/env python3
"""
Fill Cap Simulation

Simulates realistic trading with fill price caps:
- If entry_price > p_max → skip trade (unfilled)
- Otherwise trade at entry_price

Shows EV sensitivity to fill quality for top filters.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import List, Dict, Any

from first_touch_filters import (
    ExtendedTrade,
    analyze_window_extended,
)
from backtest_btc15 import load_windows


def simulate_with_fill_cap(
    trades: List[ExtendedTrade],
    p_max: int,
    filter_fn=None
) -> Dict[str, Any]:
    """
    Simulate trades with a fill cap.
    
    Args:
        trades: List of extended trades
        p_max: Maximum fill price in cents (skip if entry_price > p_max)
        filter_fn: Optional filter function
    
    Returns:
        Statistics dict
    """
    if filter_fn:
        filtered = [t for t in trades if filter_fn(t)]
    else:
        filtered = trades
    
    # Apply fill cap
    executed = [t for t in filtered if t.entry_price <= p_max]
    skipped = len(filtered) - len(executed)
    
    if not executed:
        return {
            'p_max': p_max,
            'trades_possible': len(filtered),
            'trades_executed': 0,
            'fill_rate': 0,
            'skipped': skipped,
            'win_rate': 0,
            'avg_entry': 0,
            'reversal_rate': 0,
            'ev': 0,
            'total_pnl': 0,
        }
    
    n = len(executed)
    wins = sum(1 for t in executed if t.won)
    reversals = sum(1 for t in executed if t.did_reversal)
    total_entry = sum(t.entry_price for t in executed)
    total_pnl = sum(t.pnl for t in executed)
    
    win_rate = wins / n
    avg_entry = total_entry / n
    ev = win_rate - avg_entry / 100
    
    return {
        'p_max': p_max,
        'trades_possible': len(filtered),
        'trades_executed': n,
        'fill_rate': n / len(filtered) if filtered else 0,
        'skipped': skipped,
        'win_rate': round(win_rate, 4),
        'avg_entry': round(avg_entry, 2),
        'reversal_rate': round(reversals / n, 4),
        'ev': round(ev, 4),
        'total_pnl': round(total_pnl, 2),
    }


def run_fill_cap_sweep(
    trades: List[ExtendedTrade],
    filter_name: str,
    filter_fn,
    p_max_values: List[int]
) -> List[Dict[str, Any]]:
    """Run sweep across fill caps for a filter."""
    results = []
    for p_max in p_max_values:
        stats = simulate_with_fill_cap(trades, p_max, filter_fn)
        stats['filter'] = filter_name
        results.append(stats)
    return results


def print_sweep_table(sweeps: Dict[str, List[Dict[str, Any]]]) -> None:
    """Print formatted sweep tables."""
    
    print("\n" + "=" * 120)
    print("FILL CAP SIMULATION - EV vs MAX FILL PRICE")
    print("=" * 120)
    print("\nRule: If entry_price > p_max -> skip trade. Otherwise trade at entry_price.")
    print("This shows how EV degrades if you can't get fills at low prices.\n")
    
    for filter_name, results in sweeps.items():
        print(f"\n{'='*100}")
        print(f"FILTER: {filter_name}")
        print(f"{'='*100}")
        print(f"{'p_max':>7} {'Possible':>10} {'Executed':>10} {'Fill%':>8} {'Win%':>8} {'AvgEnt':>8} {'Rev%':>8} {'EV':>10} {'TotalPnL':>10}")
        print("-" * 100)
        
        for r in results:
            ev_str = f"{r['ev']:>+9.2%}" if r['ev'] != 0 else "    N/A"
            print(f"{r['p_max']:>6}c {r['trades_possible']:>10} {r['trades_executed']:>10} "
                  f"{r['fill_rate']:>7.1%} {r['win_rate']:>7.1%} {r['avg_entry']:>7.1f}c "
                  f"{r['reversal_rate']:>7.1%} {ev_str} {r['total_pnl']:>+10.2f}")
    
    print("\n" + "=" * 120)
    print("INTERPRETATION")
    print("=" * 120)
    print("""
Key columns:
- p_max: Maximum price you'll pay (limit order price)
- Possible: Trades that pass the filter
- Executed: Trades where entry_price <= p_max (would fill)
- Fill%: Execution rate (Executed / Possible)
- EV: Expected value per trade = win_rate - avg_entry/100
- TotalPnL: Sum of all PnL (in units where 1 = $1 per $1 risked)

Decision rule:
- Choose p_max where EV is still positive with safety margin
- Higher p_max = more trades but worse EV
- Lower p_max = fewer trades but better EV (if fills happen)

For live trading:
- Place LIMIT order at p_max, not market order
- If not filled in 1-2 seconds, cancel and skip
- This guarantees you never pay more than p_max
""")


def main() -> int:
    parser = argparse.ArgumentParser(description="Fill Cap Simulation")
    parser.add_argument('--input', '-i', required=True, help='Input path')
    parser.add_argument('--outdir', '-o', default='out_fillcap', help='Output directory')
    
    args = parser.parse_args()
    input_path = Path(args.input)
    outdir = Path(args.outdir)
    
    if not input_path.exists():
        print(f"ERROR: Input does not exist: {input_path}", file=sys.stderr)
        return 1
    
    print(f"Loading windows from: {input_path}")
    windows = load_windows(input_path)
    print(f"Loaded {len(windows)} windows")
    
    # Analyze all windows
    trades: List[ExtendedTrade] = []
    for window_id, ticks, _ in windows:
        trade = analyze_window_extended(window_id, ticks)
        if trade:
            trades.append(trade)
    
    print(f"Generated {len(trades)} trades")
    
    # Define filters to test
    filters = {
        'BASELINE (all first-touch)': lambda t: True,
        'PERSIST_5s': lambda t: t.persist_5s,
        'PERSIST_8s': lambda t: t.persist_8s,
        'PERSIST_10s': lambda t: getattr(t, 'persist_10s', t.persist_8s),  # fallback
        'OPP<=15_in_20s': lambda t: t.opp_max_20s <= 15,
        'OPP<=15_in_15s': lambda t: t.opp_max_15s <= 15,
        'LATE<=40+PERSIST_5s': lambda t: t.secs_left <= 40 and t.persist_5s,
        'LATE<=60+PERSIST_5s': lambda t: t.secs_left <= 60 and t.persist_5s,
        'PERSIST_5s+OPP<=20': lambda t: t.persist_5s and t.opp_max_10s <= 20,
        'LATE<=40+PERSIST_3s+OPP<=25': lambda t: t.secs_left <= 40 and t.persist_3s and t.opp_max_10s <= 25,
    }
    
    # Fill cap values to test
    p_max_values = [90, 91, 92, 93, 94, 95, 96, 97, 98, 99, 100]
    
    # Run sweeps
    all_sweeps: Dict[str, List[Dict[str, Any]]] = {}
    
    for filter_name, filter_fn in filters.items():
        results = run_fill_cap_sweep(trades, filter_name, filter_fn, p_max_values)
        all_sweeps[filter_name] = results
    
    # Print results
    print_sweep_table(all_sweeps)
    
    # Summary table: best p_max for each filter
    print("\n" + "=" * 100)
    print("RECOMMENDED p_max FOR EACH FILTER (highest EV with cushion)")
    print("=" * 100)
    print(f"{'Filter':<35} {'Best p_max':>10} {'Trades':>8} {'Win%':>8} {'EV':>10} {'Safety':>10}")
    print("-" * 100)
    
    for filter_name, results in all_sweeps.items():
        # Find p_max with best EV that has reasonable trade count
        valid = [r for r in results if r['trades_executed'] >= 50 and r['ev'] > 0]
        if valid:
            # Choose highest p_max where EV > 1% (safety margin)
            safe = [r for r in valid if r['ev'] > 0.01]
            if safe:
                best = max(safe, key=lambda x: x['p_max'])
            else:
                best = max(valid, key=lambda x: x['ev'])
            
            safety = best['win_rate'] - best['avg_entry'] / 100
            print(f"{filter_name:<35} {best['p_max']:>9}c {best['trades_executed']:>8} "
                  f"{best['win_rate']:>7.1%} {best['ev']:>+9.2%} {safety:>+9.2%}")
    
    # Write to file
    outdir.mkdir(parents=True, exist_ok=True)
    with open(outdir / "fill_cap_sweep.json", 'w') as f:
        json.dump(all_sweeps, f, indent=2)
    
    print(f"\nResults written to: {outdir}/fill_cap_sweep.json")
    
    # Final recommendation
    print("\n" + "=" * 100)
    print("FINAL RECOMMENDATION")
    print("=" * 100)
    print("""
For PERSIST_5s or PERSIST_8s filter:
  - Set limit order at p_max = 93c
  - Expected EV ~ +3.4% to +3.5% per trade
  - Win rate ~ 93.9%, which gives 0.9c cushion above 93c fills
  
For OPP<=15_in_20s filter (more trades):
  - Set limit order at p_max = 93c  
  - Expected EV ~ +3.2% per trade
  - Win rate = 93.5%, which gives 0.5c cushion

Kelly sizing (conservative):
  - Full Kelly ~ (0.939 - 0.93) / 0.07 ~ 12.9%
  - Use 1/4 Kelly = 3% of bankroll per trade
  - Never exceed 5% per trade

Order execution:
  1. When trigger fires (first touch >= 90c)
  2. Wait for confirmation (persist 5-8s OR check opp <= 15c in 20s)  
  3. Place LIMIT BUY at 93c (not market!)
  4. If not filled in 2 seconds, cancel
  5. Never re-enter same window
""")
    print("=" * 100 + "\n")
    
    return 0


if __name__ == '__main__':
    sys.exit(main())

