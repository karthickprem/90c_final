#!/usr/bin/env python3
"""
Gap Risk Mitigations

Implements:
- JUMP_GATE: Volatility filter on tick-to-tick movement
- TWOSIDED_GATE: Opposite side constraints
- CIRCUIT_BREAKER: Pause after gap losses
- HEDGE: Buy opposite on warning

Goal: Reduce gap_count while preserving EV.
"""

from __future__ import annotations

import argparse
import csv
import json
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import List, Dict, Any, Optional, Tuple
from itertools import product

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
    pre_ticks: List[Tick] = field(default_factory=list)
    post_ticks: List[Tick] = field(default_factory=list)


@dataclass
class TradeResult:
    window_id: str
    pnl_invested: float
    is_gap: bool  # loss <= -0.15
    is_severe_gap: bool  # loss <= -0.25
    exit_reason: str
    hedge_pnl: float = 0.0
    combined_pnl: float = 0.0


@dataclass
class StrategyStats:
    name: str
    params: Dict[str, Any]
    trades: int
    ev_invested: float
    worst_loss: float
    worst_1pct: float
    worst_05pct: float
    gap_count: int  # loss <= -0.15
    severe_gap_count: int  # loss <= -0.25
    max_dd_2pct: float
    max_dd_3pct: float
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


def get_opp_price(tick: Tick, side: str) -> int:
    return tick.down_cents if side == 'UP' else tick.up_cents


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
            return None  # TIE
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
        pre_ticks=[t for t in sorted_ticks if t.elapsed_seconds < trigger_time],
        post_ticks=[t for t in sorted_ticks if t.elapsed_seconds > trigger_time],
    )


# ============================================================================
# Gate Functions
# ============================================================================

def check_spike_filter(ctx: TradeContext, floor: int = 88, push: int = 93, m: float = 10) -> Tuple[bool, int, int]:
    """Check SPIKE filter and return (passed, min, max)."""
    end_time = ctx.trigger_time + m
    prices = [get_price(t, ctx.first_touch_side) 
              for t in ctx.post_ticks if t.elapsed_seconds <= end_time]
    if not prices:
        return (ctx.trigger_price >= floor and ctx.trigger_price >= push,
                ctx.trigger_price, ctx.trigger_price)
    min_p, max_p = min(prices), max(prices)
    return (min_p >= floor and max_p >= push, min_p, max_p)


def check_jump_gate(ctx: TradeContext, jump_cents: int, m: float = 10) -> bool:
    """
    JUMP_GATE: Reject if max tick-to-tick delta >= jump_cents.
    Computed for BOTH sides during validation window.
    Returns True if OK to trade (no excessive jumps).
    """
    end_time = ctx.trigger_time + m
    window_ticks = [t for t in ctx.post_ticks if t.elapsed_seconds <= end_time]
    
    if len(window_ticks) < 2:
        return True  # Not enough data
    
    max_delta = 0
    for i in range(1, len(window_ticks)):
        prev, curr = window_ticks[i-1], window_ticks[i]
        
        # Check both sides
        delta_up = abs(curr.up_cents - prev.up_cents)
        delta_down = abs(curr.down_cents - prev.down_cents)
        max_delta = max(max_delta, delta_up, delta_down)
    
    return max_delta < jump_cents


def check_twosided_gate(ctx: TradeContext, opp_max: int, opp_slope: int, m: float = 10) -> bool:
    """
    TWOSIDED_GATE: Reject if opposite gets too high or rises too fast.
    Returns True if OK to trade.
    """
    end_time = ctx.trigger_time + m
    window_ticks = [t for t in ctx.post_ticks if t.elapsed_seconds <= end_time]
    
    if not window_ticks:
        return True
    
    opp_prices = [get_opp_price(t, ctx.first_touch_side) for t in window_ticks]
    
    # Check max
    if max(opp_prices) > opp_max:
        return False
    
    # Check slope (first to last in window)
    if len(opp_prices) >= 2:
        delta = opp_prices[-1] - opp_prices[0]
        if delta > opp_slope:
            return False
    
    return True


# ============================================================================
# Exit Simulation with Hedge
# ============================================================================

def simulate_trade(
    ctx: TradeContext,
    entry_price: int,
    entry_time: float,
    sl: int = 86,
    tp: int = 97,
    slip_exit: int = 1,
    hedge_frac: float = 0.0,
    hedge_trigger: int = 25,
    hedge_window: float = 30,
) -> Tuple[float, str, float, float]:
    """
    Simulate trade with optional hedge.
    
    Returns: (pnl_invested, exit_reason, hedge_pnl, combined_pnl)
    """
    side = ctx.first_touch_side
    hedge_entry_price = None
    hedge_triggered = False
    
    for t in ctx.post_ticks:
        if t.elapsed_seconds <= entry_time:
            continue
        
        self_price = get_price(t, side)
        opp_price = get_opp_price(t, side)
        
        # Check hedge trigger
        if hedge_frac > 0 and not hedge_triggered:
            if t.elapsed_seconds <= entry_time + hedge_window:
                if opp_price >= hedge_trigger:
                    hedge_entry_price = opp_price + 1  # Slip on hedge entry
                    hedge_triggered = True
        
        # TP
        if self_price >= tp:
            main_pnl = (tp - entry_price) / entry_price
            hedge_pnl = compute_hedge_pnl(hedge_entry_price, ctx.winner, side, hedge_frac) if hedge_triggered else 0
            combined = main_pnl * (1 - hedge_frac) + hedge_pnl * hedge_frac if hedge_triggered else main_pnl
            return main_pnl, 'TAKE_PROFIT', hedge_pnl, combined
        
        # SL
        if self_price <= sl:
            exit_price = max(0, self_price - slip_exit)
            main_pnl = (exit_price - entry_price) / entry_price
            hedge_pnl = compute_hedge_pnl(hedge_entry_price, ctx.winner, side, hedge_frac) if hedge_triggered else 0
            combined = main_pnl * (1 - hedge_frac) + hedge_pnl * hedge_frac if hedge_triggered else main_pnl
            return main_pnl, 'STOP_LOSS', hedge_pnl, combined
    
    # Settlement
    if ctx.winner == side:
        main_pnl = (100 - entry_price) / entry_price
    else:
        main_pnl = (0 - entry_price) / entry_price
    
    hedge_pnl = compute_hedge_pnl(hedge_entry_price, ctx.winner, side, hedge_frac) if hedge_triggered else 0
    combined = main_pnl * (1 - hedge_frac) + hedge_pnl * hedge_frac if hedge_triggered else main_pnl
    
    return main_pnl, 'SETTLEMENT', hedge_pnl, combined


def compute_hedge_pnl(hedge_entry: Optional[int], winner: str, main_side: str, hedge_frac: float) -> float:
    """Compute hedge PnL (buying opposite side)."""
    if hedge_entry is None:
        return 0.0
    
    opp_side = 'DOWN' if main_side == 'UP' else 'UP'
    
    if winner == opp_side:
        # Hedge won
        return (100 - hedge_entry) / hedge_entry
    else:
        # Hedge lost
        return -1.0  # Lost entire hedge position


# ============================================================================
# Strategy Evaluation
# ============================================================================

@dataclass
class StrategyConfig:
    name: str
    # Base
    spike_floor: int = 88
    spike_push: int = 93
    spike_m: float = 10
    p_max: int = 93
    slip_entry: int = 1
    slip_exit: int = 1
    sl: int = 86
    tp: int = 97
    # Gates
    jump_gate: Optional[int] = None  # JUMP_CENTS
    opp_max: Optional[int] = None
    opp_slope: Optional[int] = None
    # Hedge
    hedge_frac: float = 0.0
    hedge_trigger: int = 25


def evaluate_with_circuit_breaker(
    contexts: List[TradeContext],
    config: StrategyConfig,
    circuit_k: int = 0,
    daily_dd_limit: float = -0.05,
) -> StrategyStats:
    """Evaluate strategy with circuit breaker simulation."""
    
    trades: List[TradeResult] = []
    skip_until_window = 0
    daily_pnl = 0.0
    current_day = None
    
    for ctx in contexts:
        # Parse day from window_id (format: YY_MM_DD_HH_MM_...)
        parts = ctx.window_id.split('_')
        if len(parts) >= 3:
            day = '_'.join(parts[:3])
        else:
            day = ctx.window_id
        
        # Reset daily tracking
        if day != current_day:
            current_day = day
            daily_pnl = 0.0
        
        # Check daily circuit breaker
        if daily_pnl <= daily_dd_limit:
            continue
        
        # Check window skip (from gap circuit breaker)
        window_num = hash(ctx.window_id) % 100000  # Proxy for ordering
        if window_num < skip_until_window:
            continue
        
        # SPIKE filter
        passed, _, _ = check_spike_filter(ctx, config.spike_floor, config.spike_push, config.spike_m)
        if not passed:
            continue
        
        # Entry price check
        if ctx.trigger_price > config.p_max:
            continue
        
        entry_price = min(ctx.trigger_price + config.slip_entry, config.p_max)
        entry_time = ctx.trigger_time + config.spike_m
        
        # JUMP_GATE
        if config.jump_gate and not check_jump_gate(ctx, config.jump_gate, config.spike_m):
            continue
        
        # TWOSIDED_GATE
        if config.opp_max is not None and config.opp_slope is not None:
            if not check_twosided_gate(ctx, config.opp_max, config.opp_slope, config.spike_m):
                continue
        
        # Simulate trade
        pnl, exit_reason, hedge_pnl, combined_pnl = simulate_trade(
            ctx, entry_price, entry_time,
            sl=config.sl, tp=config.tp, slip_exit=config.slip_exit,
            hedge_frac=config.hedge_frac, hedge_trigger=config.hedge_trigger,
        )
        
        is_gap = pnl <= -0.15
        is_severe = pnl <= -0.25
        
        trades.append(TradeResult(
            window_id=ctx.window_id,
            pnl_invested=pnl,
            is_gap=is_gap,
            is_severe_gap=is_severe,
            exit_reason=exit_reason,
            hedge_pnl=hedge_pnl,
            combined_pnl=combined_pnl,
        ))
        
        # Update daily PnL
        daily_pnl += pnl * 0.03  # Assuming 3% position
        
        # Gap circuit breaker
        if is_gap and circuit_k > 0:
            skip_until_window = window_num + circuit_k
    
    # Compute stats
    n = len(trades)
    if n == 0:
        return StrategyStats(
            name=config.name, params={}, trades=0,
            ev_invested=0, worst_loss=0, worst_1pct=0, worst_05pct=0,
            gap_count=0, severe_gap_count=0,
            max_dd_2pct=0, max_dd_3pct=0, profit_factor=0,
        )
    
    pnls = [t.pnl_invested for t in trades]
    ev_invested = sum(pnls) / n
    
    sorted_pnls = sorted(pnls)
    worst_loss = sorted_pnls[0]
    worst_1pct = sorted_pnls[max(0, int(n * 0.01))]
    worst_05pct = sorted_pnls[max(0, int(n * 0.005))]
    
    gap_count = sum(1 for t in trades if t.is_gap)
    severe_gap_count = sum(1 for t in trades if t.is_severe_gap)
    
    # Drawdown simulation
    def simulate_dd(f: float) -> float:
        bankroll, peak, max_dd = 1.0, 1.0, 0.0
        for t in trades:
            bankroll *= (1 + f * t.pnl_invested)
            peak = max(peak, bankroll)
            dd = (peak - bankroll) / peak
            max_dd = max(max_dd, dd)
        return max_dd
    
    max_dd_2pct = simulate_dd(0.02)
    max_dd_3pct = simulate_dd(0.03)
    
    # Profit factor
    gross_profit = sum(p for p in pnls if p > 0)
    gross_loss = abs(sum(p for p in pnls if p < 0))
    profit_factor = gross_profit / gross_loss if gross_loss > 0 else float('inf')
    
    return StrategyStats(
        name=config.name,
        params={
            'jump_gate': config.jump_gate,
            'opp_max': config.opp_max,
            'opp_slope': config.opp_slope,
            'hedge_frac': config.hedge_frac,
        },
        trades=n,
        ev_invested=round(ev_invested, 4),
        worst_loss=round(worst_loss, 4),
        worst_1pct=round(worst_1pct, 4),
        worst_05pct=round(worst_05pct, 4),
        gap_count=gap_count,
        severe_gap_count=severe_gap_count,
        max_dd_2pct=round(max_dd_2pct, 4),
        max_dd_3pct=round(max_dd_3pct, 4),
        profit_factor=round(profit_factor, 2),
    )


def main() -> int:
    parser = argparse.ArgumentParser(description="Gap Risk Mitigations")
    parser.add_argument('--input', '-i', required=True, help='Input path')
    parser.add_argument('--outdir', '-o', default='out_gap_risk', help='Output directory')
    
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
    print(f"Built {len(contexts)} trade contexts")
    
    # Generate configurations
    configs = []
    
    # Baseline
    configs.append(StrategyConfig(name="BASELINE"))
    
    # JUMP_GATE sweep
    for jump in [6, 8, 10, 12]:
        configs.append(StrategyConfig(name=f"JUMP_{jump}c", jump_gate=jump))
    
    # TWOSIDED_GATE sweep
    for opp_max in [15, 20, 25]:
        for opp_slope in [5, 8, 10]:
            configs.append(StrategyConfig(
                name=f"TWOSIDED_max{opp_max}_slope{opp_slope}",
                opp_max=opp_max, opp_slope=opp_slope
            ))
    
    # Combined: JUMP + TWOSIDED
    for jump in [8, 10]:
        for opp_max in [20, 25]:
            configs.append(StrategyConfig(
                name=f"JUMP_{jump}c_OPP{opp_max}",
                jump_gate=jump, opp_max=opp_max, opp_slope=8
            ))
    
    # HEDGE variants
    for hedge in [0.1, 0.2]:
        configs.append(StrategyConfig(
            name=f"HEDGE_{int(hedge*100)}pct",
            hedge_frac=hedge, hedge_trigger=25
        ))
    
    # Best combo with hedge
    configs.append(StrategyConfig(
        name="JUMP_8c_OPP20_HEDGE10",
        jump_gate=8, opp_max=20, opp_slope=8, hedge_frac=0.1
    ))
    
    print(f"Evaluating {len(configs)} strategies...")
    
    results: List[StrategyStats] = []
    for config in configs:
        result = evaluate_with_circuit_breaker(contexts, config)
        results.append(result)
    
    # Sort by EV
    results.sort(key=lambda r: r.ev_invested, reverse=True)
    
    # Print results
    print("\n" + "=" * 140)
    print("GAP RISK MITIGATION RESULTS")
    print("=" * 140)
    print(f"{'Rank':>4} {'Strategy':<35} {'Trades':>7} {'EV/inv':>8} {'WorstL':>8} {'W1%':>7} "
          f"{'Gaps':>5} {'SevGap':>6} {'DD@3%':>7} {'PF':>6}")
    print("-" * 140)
    
    for i, r in enumerate(results):
        print(f"{i+1:>4} {r.name:<35} {r.trades:>7} {r.ev_invested:>+7.2%} "
              f"{r.worst_loss:>+7.2%} {r.worst_1pct:>+6.2%} "
              f"{r.gap_count:>5} {r.severe_gap_count:>6} {r.max_dd_3pct:>6.2%} {r.profit_factor:>6.2f}")
    
    # Find baseline
    baseline = next((r for r in results if r.name == "BASELINE"), None)
    
    # Pareto frontier: EV vs Gap Count
    print("\n" + "=" * 100)
    print("PARETO FRONTIER (EV vs Gap Count)")
    print("=" * 100)
    print("\nStrategies that reduce gaps with minimal EV loss:\n")
    
    if baseline:
        print(f"Baseline: {baseline.trades} trades, EV={baseline.ev_invested:+.2%}, "
              f"Gaps={baseline.gap_count}, Severe={baseline.severe_gap_count}")
        print()
        
        # Find Pareto-optimal strategies
        pareto = []
        for r in results:
            if r.trades < 500:
                continue
            is_dominated = False
            for other in results:
                if other.trades < 500:
                    continue
                # Dominated if other has higher EV AND fewer gaps
                if other.ev_invested > r.ev_invested and other.gap_count < r.gap_count:
                    is_dominated = True
                    break
            if not is_dominated:
                pareto.append(r)
        
        pareto.sort(key=lambda r: r.gap_count)
        
        print(f"{'Strategy':<35} {'Trades':>7} {'EV/inv':>8} {'dEV':>8} {'Gaps':>5} {'dGaps':>6}")
        print("-" * 75)
        
        for r in pareto[:10]:
            d_ev = r.ev_invested - baseline.ev_invested
            d_gaps = r.gap_count - baseline.gap_count
            print(f"{r.name:<35} {r.trades:>7} {r.ev_invested:>+7.2%} {d_ev:>+7.2%} "
                  f"{r.gap_count:>5} {d_gaps:>+6}")
    
    # JUMP_GATE analysis
    print("\n" + "=" * 100)
    print("JUMP_GATE SWEEP")
    print("=" * 100)
    
    jump_results = [r for r in results if r.name.startswith("JUMP_") and "OPP" not in r.name]
    
    print(f"\n{'JUMP_CENTS':>12} {'Trades':>8} {'EV/inv':>9} {'Gaps':>6} {'SevGaps':>8} {'Gap%':>7}")
    print("-" * 55)
    
    if baseline:
        print(f"{'None':>12} {baseline.trades:>8} {baseline.ev_invested:>+8.2%} "
              f"{baseline.gap_count:>6} {baseline.severe_gap_count:>8} "
              f"{baseline.gap_count/baseline.trades*100:>6.2f}%")
    
    for r in sorted(jump_results, key=lambda x: int(x.name.split('_')[1].replace('c',''))):
        gap_pct = r.gap_count / r.trades * 100 if r.trades > 0 else 0
        print(f"{r.params.get('jump_gate', 'N/A'):>12} {r.trades:>8} {r.ev_invested:>+8.2%} "
              f"{r.gap_count:>6} {r.severe_gap_count:>8} {gap_pct:>6.2f}%")
    
    # TWOSIDED analysis
    print("\n" + "=" * 100)
    print("TWOSIDED_GATE SWEEP")
    print("=" * 100)
    
    two_results = [r for r in results if r.name.startswith("TWOSIDED_")]
    
    print(f"\n{'OPP_MAX':>8} {'SLOPE':>6} {'Trades':>7} {'EV/inv':>8} {'Gaps':>5} {'SevGaps':>7}")
    print("-" * 50)
    
    for r in sorted(two_results, key=lambda x: (x.params.get('opp_max', 0), x.params.get('opp_slope', 0))):
        print(f"{r.params.get('opp_max', 'N/A'):>8} {r.params.get('opp_slope', 'N/A'):>6} "
              f"{r.trades:>7} {r.ev_invested:>+7.2%} {r.gap_count:>5} {r.severe_gap_count:>7}")
    
    # Best recommendation
    print("\n" + "=" * 100)
    print("RECOMMENDED CONFIGURATION")
    print("=" * 100)
    
    # Find best: minimize gaps while keeping EV > 2% and trades > 800
    candidates = [r for r in results if r.ev_invested > 0.02 and r.trades > 800]
    if candidates:
        best = min(candidates, key=lambda r: r.gap_count)
        
        print(f"""
BEST GAP-REDUCED STRATEGY: {best.name}

Parameters:
  - SPIKE: floor=88, push=93, m=10s
  - JUMP_GATE: {best.params.get('jump_gate', 'None')}c max tick delta
  - TWOSIDED_GATE: opp_max={best.params.get('opp_max', 'None')}, opp_slope={best.params.get('opp_slope', 'None')}
  - HEDGE: {best.params.get('hedge_frac', 0)*100:.0f}%

Performance:
  - Trades: {best.trades}
  - EV/invested: {best.ev_invested:+.2%}
  - Worst Loss: {best.worst_loss:+.2%}
  - Worst 1%: {best.worst_1pct:+.2%}
  - Gap Count (<-15%): {best.gap_count}
  - Severe Gap Count (<-25%): {best.severe_gap_count}
  - Max DD @3%: {best.max_dd_3pct:.2%}
  - Profit Factor: {best.profit_factor:.2f}

Improvement vs Baseline:
  - Gap reduction: {baseline.gap_count - best.gap_count} fewer gaps ({(1 - best.gap_count/baseline.gap_count)*100:.1f}% reduction)
  - EV change: {best.ev_invested - baseline.ev_invested:+.2%}
""")
    
    # Write results
    with open(outdir / 'gap_mitigation_results.csv', 'w', newline='') as f:
        writer = csv.writer(f)
        writer.writerow(['name', 'trades', 'ev_invested', 'worst_loss', 'worst_1pct',
                        'gap_count', 'severe_gap_count', 'max_dd_3pct', 'profit_factor'])
        for r in results:
            writer.writerow([r.name, r.trades, r.ev_invested, r.worst_loss, r.worst_1pct,
                            r.gap_count, r.severe_gap_count, r.max_dd_3pct, r.profit_factor])
    
    print(f"\nResults written to: {outdir}/gap_mitigation_results.csv")
    
    return 0


if __name__ == '__main__':
    sys.exit(main())


