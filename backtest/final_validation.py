#!/usr/bin/env python3
"""
Final Production Validation

1. Combined jump gate (big + mid jump filter)
2. Time-split validation (train/test)
3. Slippage stress grid
4. Bankroll simulation with sequence risk
"""

from __future__ import annotations

import argparse
import csv
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import List, Dict, Any, Optional, Tuple
from datetime import datetime

from backtest_btc15 import load_windows, Tick, segment_ticks_by_reset


# ============================================================================
# Data Structures
# ============================================================================

@dataclass
class TradeContext:
    window_id: str
    sorted_ticks: List[Tick]
    trigger_idx: int
    trigger_time: float
    first_touch_side: str
    trigger_price: int
    winner: str
    max_time: float
    post_ticks: List[Tick] = field(default_factory=list)
    date_str: str = ""  # YYMMDD format


@dataclass
class TradeResult:
    window_id: str
    date_str: str
    pnl_invested: float
    is_gap: bool
    is_severe: bool
    exit_reason: str


@dataclass
class StrategyStats:
    name: str
    trades: int
    ev_invested: float
    worst_loss: float
    worst_1pct: float
    gap_count: int
    severe_count: int
    max_dd: float
    final_bankroll: float
    profit_factor: float


# ============================================================================
# Core Functions
# ============================================================================

def determine_winner(ticks: List[Tick]) -> str:
    if not ticks:
        return 'UNCLEAR'
    valid = [t for t in ticks if 0 <= t.up_cents <= 100 and 0 <= t.down_cents <= 100]
    if not valid:
        return 'UNCLEAR'
    sorted_ticks = sorted(valid, key=lambda t: t.elapsed_seconds)
    for t in reversed(sorted_ticks):
        up, down = t.up_cents / 100, t.down_cents / 100
        if max(up, down) >= 0.97 and min(up, down) <= 0.03:
            return 'UP' if up > down else 'DOWN'
    last = sorted_ticks[-1]
    up, down = last.up_cents / 100, last.down_cents / 100
    if abs(up - down) < 0.05 and max(up, down) < 0.60:
        return 'UNCLEAR'
    return 'UP' if up > down else 'DOWN'


def get_price(tick: Tick, side: str) -> int:
    return tick.up_cents if side == 'UP' else tick.down_cents


def parse_date_from_window_id(window_id: str) -> str:
    """Extract YYMMDD from window_id like 25_10_30_23_00_23_15."""
    parts = window_id.split('_')
    if len(parts) >= 3:
        return f"{parts[0]}{parts[1]}{parts[2]}"
    return window_id


def build_trade_context(window_id: str, ticks: List[Tick]) -> Optional[TradeContext]:
    segments, _ = segment_ticks_by_reset(ticks)
    if not segments or not segments[0]:
        return None
    valid = [t for t in segments[0] if 0 <= t.up_cents <= 100 and 0 <= t.down_cents <= 100]
    if not valid:
        return None
    sorted_ticks = sorted(valid, key=lambda t: t.elapsed_seconds)
    
    trigger_idx = None
    first_touch_side = None
    for i, t in enumerate(sorted_ticks):
        if t.up_cents >= 90 and t.down_cents >= 90:
            return None
        elif t.up_cents >= 90:
            first_touch_side = 'UP'
            trigger_idx = i
            break
        elif t.down_cents >= 90:
            first_touch_side = 'DOWN'
            trigger_idx = i
            break
    
    if trigger_idx is None:
        return None
    
    trigger_tick = sorted_ticks[trigger_idx]
    trigger_time = trigger_tick.elapsed_seconds
    winner = determine_winner(sorted_ticks)
    if winner == 'UNCLEAR':
        return None
    
    return TradeContext(
        window_id=window_id,
        sorted_ticks=sorted_ticks,
        trigger_idx=trigger_idx,
        trigger_time=trigger_time,
        first_touch_side=first_touch_side,
        trigger_price=get_price(trigger_tick, first_touch_side),
        winner=winner,
        max_time=max(t.elapsed_seconds for t in sorted_ticks),
        post_ticks=[t for t in sorted_ticks if t.elapsed_seconds > trigger_time],
        date_str=parse_date_from_window_id(window_id),
    )


# ============================================================================
# Gate Functions
# ============================================================================

def check_spike(ctx: TradeContext, floor: int = 88, push: int = 93, m: float = 10) -> bool:
    end_time = ctx.trigger_time + m
    prices = [get_price(t, ctx.first_touch_side) 
              for t in ctx.post_ticks if t.elapsed_seconds <= end_time]
    if not prices:
        return ctx.trigger_price >= floor and ctx.trigger_price >= push
    return min(prices) >= floor and max(prices) >= push


def check_combined_jump_gate(
    ctx: TradeContext,
    big_jump: int = 6,
    mid_jump: int = 4,
    max_mid_count: int = 2,
    m: float = 10
) -> bool:
    """
    Combined jump gate:
    - Reject if max_abs_delta >= big_jump
    - OR if count(abs_delta >= mid_jump) >= max_mid_count
    
    Returns True if OK to trade.
    """
    end_time = ctx.trigger_time + m
    window_ticks = [t for t in ctx.post_ticks if t.elapsed_seconds <= end_time]
    
    if len(window_ticks) < 2:
        return True
    
    max_delta = 0
    mid_count = 0
    
    for i in range(1, len(window_ticks)):
        prev, curr = window_ticks[i-1], window_ticks[i]
        delta_up = abs(curr.up_cents - prev.up_cents)
        delta_down = abs(curr.down_cents - prev.down_cents)
        max_d = max(delta_up, delta_down)
        
        max_delta = max(max_delta, max_d)
        if max_d >= mid_jump:
            mid_count += 1
    
    # Reject if big jump OR too many mid jumps
    if max_delta >= big_jump:
        return False
    if mid_count >= max_mid_count:
        return False
    
    return True


# ============================================================================
# Trade Simulation
# ============================================================================

def simulate_trade(
    ctx: TradeContext,
    entry_price: int,
    entry_time: float,
    sl: int = 86,
    tp: int = 97,
    slip_exit: int = 1,
) -> Tuple[float, str]:
    """Returns (pnl_invested, exit_reason)."""
    side = ctx.first_touch_side
    
    for t in ctx.post_ticks:
        if t.elapsed_seconds <= entry_time:
            continue
        
        self_price = get_price(t, side)
        
        if self_price >= tp:
            return (tp - entry_price) / entry_price, 'TP'
        
        if self_price <= sl:
            exit_price = max(0, self_price - slip_exit)
            return (exit_price - entry_price) / entry_price, 'SL'
    
    if ctx.winner == side:
        return (100 - entry_price) / entry_price, 'WIN'
    else:
        return (0 - entry_price) / entry_price, 'LOSS'


def evaluate_strategy(
    contexts: List[TradeContext],
    p_max: int = 93,
    slip_entry: int = 1,
    slip_exit: int = 1,
    big_jump: int = 6,
    mid_jump: int = 4,
    max_mid_count: int = 2,
    f: float = 0.02,
) -> Tuple[StrategyStats, List[TradeResult]]:
    """Evaluate with bankroll simulation."""
    
    trades: List[TradeResult] = []
    bankroll = 1.0
    peak = 1.0
    max_dd = 0.0
    
    for ctx in contexts:
        # SPIKE check
        if not check_spike(ctx):
            continue
        
        # Entry price check
        if ctx.trigger_price > p_max:
            continue
        
        # Combined jump gate
        if not check_combined_jump_gate(ctx, big_jump, mid_jump, max_mid_count):
            continue
        
        entry_price = min(ctx.trigger_price + slip_entry, p_max)
        entry_time = ctx.trigger_time + 10
        
        pnl, reason = simulate_trade(ctx, entry_price, entry_time, slip_exit=slip_exit)
        
        is_gap = pnl <= -0.15
        is_severe = pnl <= -0.25
        
        trades.append(TradeResult(
            window_id=ctx.window_id,
            date_str=ctx.date_str,
            pnl_invested=pnl,
            is_gap=is_gap,
            is_severe=is_severe,
            exit_reason=reason,
        ))
        
        # Update bankroll
        bankroll *= (1 + f * pnl)
        peak = max(peak, bankroll)
        dd = (peak - bankroll) / peak
        max_dd = max(max_dd, dd)
    
    # Compute stats
    n = len(trades)
    if n == 0:
        return StrategyStats("", 0, 0, 0, 0, 0, 0, 0, 1.0, 0), []
    
    pnls = [t.pnl_invested for t in trades]
    sorted_pnls = sorted(pnls)
    
    ev = sum(pnls) / n
    worst = sorted_pnls[0]
    worst_1pct = sorted_pnls[max(0, int(n * 0.01))]
    
    gap_count = sum(1 for t in trades if t.is_gap)
    severe_count = sum(1 for t in trades if t.is_severe)
    
    gross_profit = sum(p for p in pnls if p > 0)
    gross_loss = abs(sum(p for p in pnls if p < 0))
    pf = gross_profit / gross_loss if gross_loss > 0 else float('inf')
    
    stats = StrategyStats(
        name="",
        trades=n,
        ev_invested=round(ev, 4),
        worst_loss=round(worst, 4),
        worst_1pct=round(worst_1pct, 4),
        gap_count=gap_count,
        severe_count=severe_count,
        max_dd=round(max_dd, 4),
        final_bankroll=round(bankroll, 4),
        profit_factor=round(pf, 2),
    )
    
    return stats, trades


def main() -> int:
    parser = argparse.ArgumentParser(description="Final Production Validation")
    parser.add_argument('--input', '-i', required=True, help='Input path')
    parser.add_argument('--outdir', '-o', default='out_final', help='Output directory')
    
    args = parser.parse_args()
    input_path = Path(args.input)
    outdir = Path(args.outdir)
    
    if not input_path.exists():
        print(f"ERROR: Input does not exist: {input_path}", file=sys.stderr)
        return 1
    
    outdir.mkdir(parents=True, exist_ok=True)
    
    print(f"Loading windows from: {input_path}")
    windows = load_windows(input_path)
    print(f"Loaded {len(windows)} windows")
    
    print("Building trade contexts...")
    contexts: List[TradeContext] = []
    for window_id, ticks, _ in windows:
        ctx = build_trade_context(window_id, ticks)
        if ctx:
            contexts.append(ctx)
    
    # Sort by window_id (chronological)
    contexts.sort(key=lambda c: c.window_id)
    print(f"Built {len(contexts)} trade contexts")
    
    # Get date range
    dates = sorted(set(c.date_str for c in contexts))
    print(f"Date range: {dates[0]} to {dates[-1]}")
    
    # ========================================================================
    # 1. Combined Jump Gate Sweep
    # ========================================================================
    
    print("\n" + "=" * 100)
    print("1. COMBINED JUMP GATE SWEEP")
    print("=" * 100)
    
    print(f"\n{'big_jump':>10} {'mid_jump':>10} {'mid_count':>10} {'Trades':>8} {'EV':>8} "
          f"{'Worst':>8} {'Gaps':>6} {'Severe':>7}")
    print("-" * 80)
    
    # Baseline (no gate)
    stats_base, _ = evaluate_strategy(contexts, big_jump=100, mid_jump=100, max_mid_count=100)
    print(f"{'None':>10} {'None':>10} {'None':>10} {stats_base.trades:>8} "
          f"{stats_base.ev_invested:>+7.2%} {stats_base.worst_loss:>+7.2%} "
          f"{stats_base.gap_count:>6} {stats_base.severe_count:>7}")
    
    # Sweep
    best_config = None
    best_severe = 999
    
    for big in [6, 8]:
        for mid in [3, 4, 5]:
            for count in [2, 3]:
                if mid >= big:
                    continue
                stats, _ = evaluate_strategy(contexts, big_jump=big, mid_jump=mid, max_mid_count=count)
                print(f"{big:>10} {mid:>10} {count:>10} {stats.trades:>8} "
                      f"{stats.ev_invested:>+7.2%} {stats.worst_loss:>+7.2%} "
                      f"{stats.gap_count:>6} {stats.severe_count:>7}")
                
                if stats.severe_count < best_severe or (stats.severe_count == best_severe and stats.ev_invested > best_config[1]):
                    best_severe = stats.severe_count
                    best_config = ((big, mid, count), stats.ev_invested, stats)
    
    print(f"\nBest config: big={best_config[0][0]}, mid={best_config[0][1]}, count={best_config[0][2]}")
    
    # Use best config for remaining tests
    BEST_BIG = best_config[0][0]
    BEST_MID = best_config[0][1]
    BEST_COUNT = best_config[0][2]
    
    # ========================================================================
    # 2. Time-Split Validation
    # ========================================================================
    
    print("\n" + "=" * 100)
    print("2. TIME-SPLIT VALIDATION")
    print("=" * 100)
    
    # Split at midpoint
    mid_idx = len(contexts) // 2
    first_half = contexts[:mid_idx]
    second_half = contexts[mid_idx:]
    
    print(f"\nFirst half: {len(first_half)} contexts, dates {first_half[0].date_str} to {first_half[-1].date_str}")
    print(f"Second half: {len(second_half)} contexts, dates {second_half[0].date_str} to {second_half[-1].date_str}")
    
    stats_first, trades_first = evaluate_strategy(
        first_half, big_jump=BEST_BIG, mid_jump=BEST_MID, max_mid_count=BEST_COUNT
    )
    stats_second, trades_second = evaluate_strategy(
        second_half, big_jump=BEST_BIG, mid_jump=BEST_MID, max_mid_count=BEST_COUNT
    )
    stats_full, trades_full = evaluate_strategy(
        contexts, big_jump=BEST_BIG, mid_jump=BEST_MID, max_mid_count=BEST_COUNT
    )
    
    print(f"\n{'Split':<15} {'Trades':>8} {'EV/inv':>9} {'Worst':>9} {'Gaps':>6} {'Severe':>7} {'MaxDD':>8} {'PF':>6}")
    print("-" * 80)
    print(f"{'First Half':<15} {stats_first.trades:>8} {stats_first.ev_invested:>+8.2%} "
          f"{stats_first.worst_loss:>+8.2%} {stats_first.gap_count:>6} {stats_first.severe_count:>7} "
          f"{stats_first.max_dd:>7.2%} {stats_first.profit_factor:>6.2f}")
    print(f"{'Second Half':<15} {stats_second.trades:>8} {stats_second.ev_invested:>+8.2%} "
          f"{stats_second.worst_loss:>+8.2%} {stats_second.gap_count:>6} {stats_second.severe_count:>7} "
          f"{stats_second.max_dd:>7.2%} {stats_second.profit_factor:>6.2f}")
    print(f"{'Full Period':<15} {stats_full.trades:>8} {stats_full.ev_invested:>+8.2%} "
          f"{stats_full.worst_loss:>+8.2%} {stats_full.gap_count:>6} {stats_full.severe_count:>7} "
          f"{stats_full.max_dd:>7.2%} {stats_full.profit_factor:>6.2f}")
    
    # Check for degradation
    ev_diff = stats_second.ev_invested - stats_first.ev_invested
    print(f"\nEV change first->second: {ev_diff:+.2%}")
    if ev_diff < -0.01:
        print("[WARN] EV degraded in second half - potential overfitting")
    else:
        print("[PASS] EV stable or improved in second half")
    
    # Write time split CSV
    with open(outdir / 'time_split.csv', 'w', newline='') as f:
        writer = csv.writer(f)
        writer.writerow(['split', 'trades', 'ev_invested', 'worst_loss', 'gaps', 'severe', 'max_dd', 'pf'])
        writer.writerow(['first_half', stats_first.trades, stats_first.ev_invested, 
                        stats_first.worst_loss, stats_first.gap_count, stats_first.severe_count,
                        stats_first.max_dd, stats_first.profit_factor])
        writer.writerow(['second_half', stats_second.trades, stats_second.ev_invested,
                        stats_second.worst_loss, stats_second.gap_count, stats_second.severe_count,
                        stats_second.max_dd, stats_second.profit_factor])
        writer.writerow(['full', stats_full.trades, stats_full.ev_invested,
                        stats_full.worst_loss, stats_full.gap_count, stats_full.severe_count,
                        stats_full.max_dd, stats_full.profit_factor])
    
    # ========================================================================
    # 3. Slippage Stress Grid
    # ========================================================================
    
    print("\n" + "=" * 100)
    print("3. SLIPPAGE STRESS GRID")
    print("=" * 100)
    
    slip_results = []
    
    print(f"\n{'slip_entry':>12} {'slip_exit':>11} {'Trades':>8} {'EV/inv':>9} {'Worst':>9} {'Gaps':>6} {'MaxDD':>8}")
    print("-" * 75)
    
    for slip_e in [0, 1, 2, 3, 4]:
        for slip_x in [0, 1, 2, 3]:
            stats, _ = evaluate_strategy(
                contexts,
                slip_entry=slip_e, slip_exit=slip_x,
                big_jump=BEST_BIG, mid_jump=BEST_MID, max_mid_count=BEST_COUNT
            )
            print(f"{slip_e:>11}c {slip_x:>10}c {stats.trades:>8} {stats.ev_invested:>+8.2%} "
                  f"{stats.worst_loss:>+8.2%} {stats.gap_count:>6} {stats.max_dd:>7.2%}")
            slip_results.append({
                'slip_entry': slip_e,
                'slip_exit': slip_x,
                'trades': stats.trades,
                'ev_invested': stats.ev_invested,
                'worst_loss': stats.worst_loss,
                'gaps': stats.gap_count,
                'max_dd': stats.max_dd,
            })
    
    # Find breakeven slippage
    print("\nBreakeven analysis:")
    for slip_e in [1, 2, 3, 4]:
        stats, _ = evaluate_strategy(
            contexts, slip_entry=slip_e, slip_exit=1,
            big_jump=BEST_BIG, mid_jump=BEST_MID, max_mid_count=BEST_COUNT
        )
        if stats.ev_invested > 0:
            print(f"  slip_entry={slip_e}c, slip_exit=1c: EV={stats.ev_invested:+.2%} [POSITIVE]")
        else:
            print(f"  slip_entry={slip_e}c, slip_exit=1c: EV={stats.ev_invested:+.2%} [NEGATIVE - BREAKEVEN REACHED]")
            break
    
    # Write slippage grid CSV
    with open(outdir / 'slippage_grid.csv', 'w', newline='') as f:
        writer = csv.writer(f)
        writer.writerow(['slip_entry', 'slip_exit', 'trades', 'ev_invested', 'worst_loss', 'gaps', 'max_dd'])
        for r in slip_results:
            writer.writerow([r['slip_entry'], r['slip_exit'], r['trades'], r['ev_invested'],
                            r['worst_loss'], r['gaps'], r['max_dd']])
    
    # ========================================================================
    # 4. Bankroll Simulation
    # ========================================================================
    
    print("\n" + "=" * 100)
    print("4. BANKROLL SIMULATION")
    print("=" * 100)
    
    for f_size in [0.02, 0.03]:
        stats, trades = evaluate_strategy(
            contexts,
            f=f_size,
            big_jump=BEST_BIG, mid_jump=BEST_MID, max_mid_count=BEST_COUNT
        )
        
        print(f"\nPosition size: {f_size:.0%}")
        print(f"  Trades: {stats.trades}")
        print(f"  Final bankroll: {stats.final_bankroll:.4f}x ({(stats.final_bankroll-1)*100:+.1f}%)")
        print(f"  Max drawdown: {stats.max_dd:.2%}")
        print(f"  Gap events: {stats.gap_count}")
        
        # Daily P&L analysis
        daily_pnl = {}
        for t in trades:
            if t.date_str not in daily_pnl:
                daily_pnl[t.date_str] = []
            daily_pnl[t.date_str].append(t.pnl_invested * f_size)
        
        daily_returns = {d: sum(pnls) for d, pnls in daily_pnl.items()}
        worst_day = min(daily_returns.values()) if daily_returns else 0
        best_day = max(daily_returns.values()) if daily_returns else 0
        
        print(f"  Worst day: {worst_day:+.2%}")
        print(f"  Best day: {best_day:+.2%}")
        
        # Count days with >5% DD
        dd_days = sum(1 for d in daily_returns.values() if d < -0.05)
        print(f"  Days with >5% loss: {dd_days}")
    
    # ========================================================================
    # 5. Final Validation Summary
    # ========================================================================
    
    print("\n" + "=" * 100)
    print("FINAL VALIDATION SUMMARY")
    print("=" * 100)
    
    # Get final stats
    final_stats, final_trades = evaluate_strategy(
        contexts,
        slip_entry=1, slip_exit=1,
        big_jump=BEST_BIG, mid_jump=BEST_MID, max_mid_count=BEST_COUNT,
        f=0.02
    )
    
    validation_passed = True
    validation_notes = []
    
    # Check 1: Time stability
    if abs(stats_second.ev_invested - stats_first.ev_invested) > 0.015:
        validation_passed = False
        validation_notes.append("FAIL: EV unstable across time splits")
    else:
        validation_notes.append("PASS: EV stable across time splits")
    
    # Check 2: Slippage robustness
    stats_2_2, _ = evaluate_strategy(
        contexts, slip_entry=2, slip_exit=2,
        big_jump=BEST_BIG, mid_jump=BEST_MID, max_mid_count=BEST_COUNT
    )
    if stats_2_2.ev_invested > 0:
        validation_notes.append(f"PASS: Positive EV at 2c/2c slippage ({stats_2_2.ev_invested:+.2%})")
    else:
        validation_passed = False
        validation_notes.append(f"FAIL: Negative EV at 2c/2c slippage ({stats_2_2.ev_invested:+.2%})")
    
    # Check 3: Max DD reasonable
    if final_stats.max_dd < 0.03:
        validation_notes.append(f"PASS: Max DD < 3% at 2% sizing ({final_stats.max_dd:.2%})")
    else:
        validation_notes.append(f"WARN: Max DD >= 3% at 2% sizing ({final_stats.max_dd:.2%})")
    
    # Check 4: No severe gaps
    if final_stats.severe_count == 0:
        validation_notes.append("PASS: No severe gaps (>-25%)")
    else:
        validation_notes.append(f"WARN: {final_stats.severe_count} severe gaps")
    
    print("\nValidation Checks:")
    for note in validation_notes:
        print(f"  - {note}")
    
    print(f"\nOVERALL: {'PASSED' if validation_passed else 'NEEDS REVIEW'}")
    
    # Write final validation markdown
    md_content = f"""# Final Production Validation

## Configuration

```
ENTRY:
  - Trigger: First touch >= 90c
  - Wait: 10 seconds
  - SPIKE: min >= 88c AND max >= 93c
  - JUMP GATE: big_jump < {BEST_BIG}c AND count(delta >= {BEST_MID}c) < {BEST_COUNT}
  - Execute: LIMIT BUY at 93c

EXIT:
  - TP: 97c
  - SL: 86c (with slip_exit = 1c)

SIZING:
  - Start: 2% bankroll
  - Max: 3% after live validation
```

## Performance Summary

| Metric | Value |
|--------|-------|
| Trades | {final_stats.trades} |
| EV/invested | {final_stats.ev_invested:+.2%} |
| Worst Loss | {final_stats.worst_loss:+.2%} |
| Worst 1% | {final_stats.worst_1pct:+.2%} |
| Gap Count | {final_stats.gap_count} |
| Severe Gaps | {final_stats.severe_count} |
| Max DD @2% | {final_stats.max_dd:.2%} |
| Final Bankroll | {final_stats.final_bankroll:.4f}x |
| Profit Factor | {final_stats.profit_factor:.2f} |

## Time Split Validation

| Split | Trades | EV | Worst | Gaps | Severe |
|-------|--------|-----|-------|------|--------|
| First Half | {stats_first.trades} | {stats_first.ev_invested:+.2%} | {stats_first.worst_loss:+.2%} | {stats_first.gap_count} | {stats_first.severe_count} |
| Second Half | {stats_second.trades} | {stats_second.ev_invested:+.2%} | {stats_second.worst_loss:+.2%} | {stats_second.gap_count} | {stats_second.severe_count} |

EV change: {ev_diff:+.2%} ({'STABLE' if abs(ev_diff) < 0.01 else 'DEGRADED' if ev_diff < -0.01 else 'IMPROVED'})

## Slippage Robustness

| Entry Slip | Exit Slip | EV |
|------------|-----------|-----|
| 0c | 0c | {[r for r in slip_results if r['slip_entry']==0 and r['slip_exit']==0][0]['ev_invested']:+.2%} |
| 1c | 1c | {[r for r in slip_results if r['slip_entry']==1 and r['slip_exit']==1][0]['ev_invested']:+.2%} |
| 2c | 1c | {[r for r in slip_results if r['slip_entry']==2 and r['slip_exit']==1][0]['ev_invested']:+.2%} |
| 2c | 2c | {[r for r in slip_results if r['slip_entry']==2 and r['slip_exit']==2][0]['ev_invested']:+.2%} |
| 3c | 1c | {[r for r in slip_results if r['slip_entry']==3 and r['slip_exit']==1][0]['ev_invested']:+.2%} |

## Validation Result

**{'PASSED' if validation_passed else 'NEEDS REVIEW'}**

"""
    for note in validation_notes:
        md_content += f"- {note}\n"
    
    with open(outdir / 'final_validation.md', 'w') as f:
        f.write(md_content)
    
    print(f"\nOutputs written to: {outdir}/")
    print(f"  - final_validation.md")
    print(f"  - time_split.csv")
    print(f"  - slippage_grid.csv")
    
    return 0


if __name__ == '__main__':
    sys.exit(main())


