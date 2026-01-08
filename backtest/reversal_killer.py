#!/usr/bin/env python3
"""
Reversal-Killer Strategy Pack for 90c FIRST_TOUCH

Comprehensive backtest with:
- Part A: Entry-time filters (persistence, spike/sticky, opposite suppression, momentum)
- Part B: Post-entry risk control (stop-loss, time-stop, take-profit)
- Part C: Execution realism (fill models, slippage)
- Part D: Opposite break early warning

All rules use only real-time information (no hindsight).
"""

from __future__ import annotations

import argparse
import csv
import json
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import List, Dict, Any, Optional, Tuple, Callable
from itertools import product

from backtest_btc15 import load_windows, Tick, segment_ticks_by_reset


# ============================================================================
# Data Structures
# ============================================================================

@dataclass
class TradeContext:
    """All tick data needed to evaluate a trade."""
    window_id: str
    sorted_ticks: List[Tick]
    trigger_idx: int
    trigger_time: float
    first_touch_side: str  # 'UP' or 'DOWN'
    trigger_price: int  # cents
    winner: str  # 'UP', 'DOWN', or 'UNCLEAR'
    max_time: float
    
    # Pre-computed features
    pre_ticks: List[Tick] = field(default_factory=list)  # before trigger
    post_ticks: List[Tick] = field(default_factory=list)  # after trigger


@dataclass 
class TradeResult:
    """Result of a single trade."""
    window_id: str
    first_touch_side: str
    trigger_price: int
    fill_price: int
    exit_price: Optional[int]  # None if held to settlement
    exit_reason: str  # 'SETTLEMENT', 'STOP_LOSS', 'TIME_STOP', 'TAKE_PROFIT', 'OPP_BREAK', 'SKIPPED'
    won: bool
    pnl: float  # actual PnL based on entry/exit
    secs_left: float
    is_reversal: bool


@dataclass
class StrategyResult:
    """Aggregate results for a strategy variant."""
    name: str
    params: Dict[str, Any]
    trades: int
    wins: int
    reversals: int
    avg_entry: float
    avg_exit: float
    win_rate: float
    reversal_rate: float
    ev_per_share: float
    ev_invested: float
    total_pnl: float
    worst_loss: float
    trade_results: List[TradeResult] = field(default_factory=list)


# ============================================================================
# Core Analysis Functions
# ============================================================================

def determine_winner(ticks: List[Tick]) -> Tuple[str, Optional[float]]:
    """Determine winner using resolution logic."""
    if not ticks:
        return 'UNCLEAR', None
    
    valid = [t for t in ticks if 0 <= t.up_cents <= 100 and 0 <= t.down_cents <= 100]
    if not valid:
        return 'UNCLEAR', None
    
    sorted_ticks = sorted(valid, key=lambda t: t.elapsed_seconds)
    
    # Look for resolved state
    for t in reversed(sorted_ticks):
        up, down = t.up_cents / 100, t.down_cents / 100
        if max(up, down) >= 0.97 and min(up, down) <= 0.03:
            return ('UP' if up > down else 'DOWN'), t.elapsed_seconds
    
    # Fallback to last valid
    last = sorted_ticks[-1]
    up, down = last.up_cents / 100, last.down_cents / 100
    if abs(up - down) < 0.05 and max(up, down) < 0.60:
        return 'UNCLEAR', None
    return ('UP' if up > down else 'DOWN'), last.elapsed_seconds


def get_price(tick: Tick, side: str) -> int:
    """Get price for a side."""
    return tick.up_cents if side == 'UP' else tick.down_cents


def get_opp_price(tick: Tick, side: str) -> int:
    """Get opposite side price."""
    return tick.down_cents if side == 'UP' else tick.up_cents


def build_trade_context(window_id: str, ticks: List[Tick], threshold: int = 90) -> Optional[TradeContext]:
    """Build trade context for a window."""
    # Segment
    segments, _ = segment_ticks_by_reset(ticks)
    if not segments or not segments[0]:
        return None
    
    valid = [t for t in segments[0] if 0 <= t.up_cents <= 100 and 0 <= t.down_cents <= 100]
    if not valid:
        return None
    
    sorted_ticks = sorted(valid, key=lambda t: t.elapsed_seconds)
    
    # Find first touch
    trigger_idx = None
    first_touch_side = None
    
    for i, t in enumerate(sorted_ticks):
        up_touched = t.up_cents >= threshold
        down_touched = t.down_cents >= threshold
        
        if up_touched and down_touched:
            # TIE - skip by default
            return None
        elif up_touched:
            first_touch_side = 'UP'
            trigger_idx = i
            break
        elif down_touched:
            first_touch_side = 'DOWN'
            trigger_idx = i
            break
    
    if trigger_idx is None:
        return None
    
    trigger_tick = sorted_ticks[trigger_idx]
    trigger_time = trigger_tick.elapsed_seconds
    trigger_price = get_price(trigger_tick, first_touch_side)
    
    winner, _ = determine_winner(sorted_ticks)
    if winner == 'UNCLEAR':
        return None
    
    max_time = max(t.elapsed_seconds for t in sorted_ticks)
    
    # Split ticks
    pre_ticks = [t for t in sorted_ticks if t.elapsed_seconds < trigger_time]
    post_ticks = [t for t in sorted_ticks if t.elapsed_seconds > trigger_time]
    
    return TradeContext(
        window_id=window_id,
        sorted_ticks=sorted_ticks,
        trigger_idx=trigger_idx,
        trigger_time=trigger_time,
        first_touch_side=first_touch_side,
        trigger_price=trigger_price,
        winner=winner,
        max_time=max_time,
        pre_ticks=pre_ticks,
        post_ticks=post_ticks,
    )


# ============================================================================
# Part A: Entry-Time Filters
# ============================================================================

def check_persistence(ctx: TradeContext, n_secs: float, threshold: int = 90) -> bool:
    """Check if side stays >= threshold for N seconds after trigger."""
    end_time = ctx.trigger_time + n_secs
    for t in ctx.post_ticks:
        if t.elapsed_seconds <= end_time:
            if get_price(t, ctx.first_touch_side) < threshold:
                return False
    return True


def check_spike_vs_sticky(ctx: TradeContext, floor: int, push: int, m_secs: float) -> bool:
    """
    Spike vs Sticky filter:
    - min_side in next M secs >= floor (doesn't dump)
    - max_side in next M secs >= push (continues pushing)
    """
    end_time = ctx.trigger_time + m_secs
    prices = [get_price(t, ctx.first_touch_side) 
              for t in ctx.post_ticks if t.elapsed_seconds <= end_time]
    
    if not prices:
        # No post ticks in window, use trigger price
        return ctx.trigger_price >= floor and ctx.trigger_price >= push
    
    return min(prices) >= floor and max(prices) >= push


def check_opp_suppression(ctx: TradeContext, x_max: int, m_secs: float) -> bool:
    """
    Opposite suppression: opposite must stay <= X cents for M seconds after trigger.
    Returns True if OK to enter.
    """
    end_time = ctx.trigger_time + m_secs
    for t in ctx.post_ticks:
        if t.elapsed_seconds <= end_time:
            if get_opp_price(t, ctx.first_touch_side) > x_max:
                return False
    return True


def check_momentum(ctx: TradeContext, d_side_min: int, d_opp_max: int, lookback: float = 20) -> bool:
    """
    Micro-momentum filter: check price changes in last 20s before trigger.
    - d_side >= d_side_min (fast run-up)
    - d_opp <= d_opp_max (opposite not rising)
    """
    start_time = ctx.trigger_time - lookback
    
    # Find prices at start and trigger
    pre_in_window = [t for t in ctx.pre_ticks if t.elapsed_seconds >= start_time]
    if not pre_in_window:
        return True  # Not enough history, allow
    
    earliest = min(pre_in_window, key=lambda t: t.elapsed_seconds)
    trigger_tick = ctx.sorted_ticks[ctx.trigger_idx]
    
    side_start = get_price(earliest, ctx.first_touch_side)
    side_end = get_price(trigger_tick, ctx.first_touch_side)
    d_side = side_end - side_start
    
    opp_start = get_opp_price(earliest, ctx.first_touch_side)
    opp_end = get_opp_price(trigger_tick, ctx.first_touch_side)
    d_opp = opp_end - opp_start
    
    return d_side >= d_side_min and d_opp <= d_opp_max


def get_secs_left(ctx: TradeContext) -> float:
    """Estimate seconds left at trigger."""
    # Assume 900s window
    return max(0, 900 - ctx.trigger_time) if ctx.max_time < 850 else max(0, ctx.max_time - ctx.trigger_time)


# ============================================================================
# Part B: Post-Entry Risk Control
# ============================================================================

def simulate_exit(
    ctx: TradeContext,
    fill_price: int,
    stop_loss: Optional[int] = None,
    time_stop: Optional[Tuple[int, float]] = None,  # (tp_confirm, t_confirm)
    take_profit: Optional[int] = None,
    opp_break: Optional[Tuple[int, float]] = None,  # (hedge_trigger, m_secs)
) -> Tuple[int, str]:
    """
    Simulate post-entry behavior and return (exit_price, exit_reason).
    
    Returns exit_price and reason:
    - 'SETTLEMENT': held to end
    - 'STOP_LOSS': side dropped below SL
    - 'TIME_STOP': no follow-through in time
    - 'TAKE_PROFIT': hit TP
    - 'OPP_BREAK': opposite rose too fast
    """
    side = ctx.first_touch_side
    
    # Track confirmation for time_stop
    confirmed = False
    confirm_deadline = None
    if time_stop:
        tp_confirm, t_confirm = time_stop
        confirm_deadline = ctx.trigger_time + t_confirm
    
    for t in ctx.post_ticks:
        self_price = get_price(t, side)
        opp_price = get_opp_price(t, side)
        
        # Check take-profit first (wins)
        if take_profit and self_price >= take_profit:
            return take_profit, 'TAKE_PROFIT'
        
        # Check stop-loss
        if stop_loss and self_price < stop_loss:
            return self_price, 'STOP_LOSS'
        
        # Check opposite break
        if opp_break:
            hedge_trigger, m_secs = opp_break
            if t.elapsed_seconds <= ctx.trigger_time + m_secs:
                if opp_price >= hedge_trigger:
                    return self_price, 'OPP_BREAK'
        
        # Track confirmation
        if time_stop and not confirmed:
            tp_confirm, _ = time_stop
            if self_price >= tp_confirm:
                confirmed = True
        
        # Check time stop
        if time_stop and not confirmed and confirm_deadline:
            if t.elapsed_seconds > confirm_deadline:
                return self_price, 'TIME_STOP'
    
    # Held to settlement
    # Final price is based on winner
    if ctx.winner == side:
        return 100, 'SETTLEMENT'  # Won, gets $1
    else:
        return 0, 'SETTLEMENT'  # Lost


# ============================================================================
# Part C: Execution Realism
# ============================================================================

def compute_fill_price(
    trigger_price: int,
    p_max: Optional[int] = None,
    slip: int = 0
) -> Optional[int]:
    """
    Compute fill price with slippage model.
    Returns None if trade should be skipped.
    """
    fill = trigger_price + slip
    
    if p_max is not None:
        if trigger_price > p_max:
            return None  # Skip trade
        fill = min(fill, p_max)
    
    return fill


# ============================================================================
# Strategy Evaluation
# ============================================================================

@dataclass
class StrategyConfig:
    """Configuration for a strategy variant."""
    name: str
    
    # Entry filters (Part A)
    persist_n: Optional[int] = None
    spike_sticky: Optional[Tuple[int, int, float]] = None  # (floor, push, m)
    opp_suppress: Optional[Tuple[int, float]] = None  # (x_max, m)
    momentum: Optional[Tuple[int, int]] = None  # (d_side_min, d_opp_max)
    late_filter: Optional[float] = None  # max secs_left
    
    # Exit rules (Part B)
    stop_loss: Optional[int] = None
    time_stop: Optional[Tuple[int, float]] = None  # (tp_confirm, t_confirm)
    take_profit: Optional[int] = None
    opp_break: Optional[Tuple[int, float]] = None  # (hedge_trigger, m)
    
    # Execution (Part C)
    p_max: Optional[int] = None
    slip: int = 0


def evaluate_strategy(
    contexts: List[TradeContext],
    config: StrategyConfig
) -> StrategyResult:
    """Evaluate a strategy on all contexts."""
    
    trade_results: List[TradeResult] = []
    
    for ctx in contexts:
        # Apply entry filters
        if config.persist_n and not check_persistence(ctx, config.persist_n):
            continue
        
        if config.spike_sticky:
            floor, push, m = config.spike_sticky
            if not check_spike_vs_sticky(ctx, floor, push, m):
                continue
        
        if config.opp_suppress:
            x_max, m = config.opp_suppress
            if not check_opp_suppression(ctx, x_max, m):
                continue
        
        if config.momentum:
            d_side_min, d_opp_max = config.momentum
            if not check_momentum(ctx, d_side_min, d_opp_max):
                continue
        
        if config.late_filter:
            if get_secs_left(ctx) > config.late_filter:
                continue
        
        # Compute fill price
        fill_price = compute_fill_price(ctx.trigger_price, config.p_max, config.slip)
        if fill_price is None:
            continue  # Skip due to price cap
        
        # Simulate exit
        exit_price, exit_reason = simulate_exit(
            ctx,
            fill_price,
            stop_loss=config.stop_loss,
            time_stop=config.time_stop,
            take_profit=config.take_profit,
            opp_break=config.opp_break,
        )
        
        # Calculate PnL
        entry_frac = fill_price / 100
        if exit_reason == 'SETTLEMENT':
            # Binary outcome
            if ctx.winner == ctx.first_touch_side:
                pnl = 1 - entry_frac  # Win: get $1, paid entry
            else:
                pnl = -entry_frac  # Lose: paid entry, get $0
            won = ctx.winner == ctx.first_touch_side
        else:
            # Early exit
            exit_frac = exit_price / 100
            pnl = exit_frac - entry_frac
            won = pnl > 0
        
        is_reversal = ctx.first_touch_side != ctx.winner
        
        trade_results.append(TradeResult(
            window_id=ctx.window_id,
            first_touch_side=ctx.first_touch_side,
            trigger_price=ctx.trigger_price,
            fill_price=fill_price,
            exit_price=exit_price if exit_reason != 'SETTLEMENT' else None,
            exit_reason=exit_reason,
            won=won,
            pnl=pnl,
            secs_left=get_secs_left(ctx),
            is_reversal=is_reversal,
        ))
    
    # Aggregate results
    n = len(trade_results)
    if n == 0:
        return StrategyResult(
            name=config.name,
            params={},
            trades=0, wins=0, reversals=0,
            avg_entry=0, avg_exit=0,
            win_rate=0, reversal_rate=0,
            ev_per_share=0, ev_invested=0,
            total_pnl=0, worst_loss=0,
        )
    
    wins = sum(1 for t in trade_results if t.won)
    reversals = sum(1 for t in trade_results if t.is_reversal)
    avg_entry = sum(t.fill_price for t in trade_results) / n
    
    exits = [t.exit_price for t in trade_results if t.exit_price is not None]
    avg_exit = sum(exits) / len(exits) if exits else 0
    
    win_rate = wins / n
    reversal_rate = reversals / n
    
    p = avg_entry / 100
    ev_per_share = win_rate - p
    ev_invested = (win_rate - p) / p if p > 0 else 0
    
    total_pnl = sum(t.pnl for t in trade_results)
    worst_loss = min(t.pnl for t in trade_results)
    
    return StrategyResult(
        name=config.name,
        params={
            'persist_n': config.persist_n,
            'spike_sticky': config.spike_sticky,
            'opp_suppress': config.opp_suppress,
            'momentum': config.momentum,
            'late_filter': config.late_filter,
            'stop_loss': config.stop_loss,
            'time_stop': config.time_stop,
            'take_profit': config.take_profit,
            'opp_break': config.opp_break,
            'p_max': config.p_max,
            'slip': config.slip,
        },
        trades=n,
        wins=wins,
        reversals=reversals,
        avg_entry=round(avg_entry, 2),
        avg_exit=round(avg_exit, 2),
        win_rate=round(win_rate, 4),
        reversal_rate=round(reversal_rate, 4),
        ev_per_share=round(ev_per_share, 4),
        ev_invested=round(ev_invested, 4),
        total_pnl=round(total_pnl, 2),
        worst_loss=round(worst_loss, 4),
        trade_results=trade_results,
    )


# ============================================================================
# Strategy Grid Generation
# ============================================================================

def generate_strategy_configs(conservative: bool = True) -> List[StrategyConfig]:
    """Generate all strategy configurations to test."""
    configs = []
    
    # Baseline
    configs.append(StrategyConfig(name="BASELINE"))
    
    # Baseline with execution
    for slip in [0, 1, 2, 3]:
        for p_max in [None, 93, 95]:
            if slip == 0 and p_max is None:
                continue  # Already have baseline
            name = f"EXEC_slip{slip}"
            if p_max:
                name += f"_pmax{p_max}"
            configs.append(StrategyConfig(name=name, p_max=p_max, slip=slip))
    
    # Part A: Persistence
    for n in [2, 3, 5, 8, 10]:
        configs.append(StrategyConfig(name=f"PERSIST_{n}s", persist_n=n))
        # With execution
        configs.append(StrategyConfig(name=f"PERSIST_{n}s_pmax93_slip1", persist_n=n, p_max=93, slip=1))
    
    # Part A: Spike vs Sticky
    for floor in [87, 88, 89]:
        for push in [92, 93, 94]:
            for m in [5, 10]:
                name = f"SPIKE_floor{floor}_push{push}_m{m}"
                configs.append(StrategyConfig(name=name, spike_sticky=(floor, push, m)))
    
    # Part A: Opposite suppression
    for x_max in [15, 20, 25, 30]:
        for m in [10, 15, 20]:
            name = f"OPP_le{x_max}_in{m}s"
            configs.append(StrategyConfig(name=name, opp_suppress=(x_max, m)))
    
    # Part A: Momentum
    for d_side in [10, 15, 20]:
        for d_opp in [0, 5, 10]:
            name = f"MOM_dside{d_side}_dopp{d_opp}"
            configs.append(StrategyConfig(name=name, momentum=(d_side, d_opp)))
    
    # Part A: Late filter
    for late in [60, 100, 200]:
        configs.append(StrategyConfig(name=f"LATE_le{late}s", late_filter=late))
    
    # Part B: Stop-loss
    for sl in [82, 84, 86, 88]:
        configs.append(StrategyConfig(name=f"SL_{sl}c", stop_loss=sl))
    
    # Part B: Time stop
    for tp_conf in [92, 93, 94]:
        for t_conf in [30, 60, 90]:
            name = f"TSTOP_tp{tp_conf}_t{t_conf}"
            configs.append(StrategyConfig(name=name, time_stop=(tp_conf, t_conf)))
    
    # Part B: Take profit
    for tp in [95, 97, 99]:
        configs.append(StrategyConfig(name=f"TP_{tp}c", take_profit=tp))
    
    # Part D: Opposite break
    for hedge in [20, 25, 30]:
        for m in [10, 20, 30]:
            name = f"OPPBRK_{hedge}c_in{m}s"
            configs.append(StrategyConfig(name=name, opp_break=(hedge, m)))
    
    # Combined strategies
    # Best persistence + execution
    for n in [5, 8, 10]:
        configs.append(StrategyConfig(
            name=f"COMBO_PERSIST{n}_pmax93_slip1",
            persist_n=n, p_max=93, slip=1
        ))
    
    # Persistence + opposite suppression
    for n in [5, 8]:
        for x_max in [15, 20, 25]:
            configs.append(StrategyConfig(
                name=f"COMBO_PERSIST{n}_OPPle{x_max}",
                persist_n=n, opp_suppress=(x_max, 20)
            ))
    
    # Persistence + opp break (exit rule)
    for n in [5, 8]:
        for hedge in [20, 25]:
            configs.append(StrategyConfig(
                name=f"COMBO_PERSIST{n}_OPPBRK{hedge}",
                persist_n=n, opp_break=(hedge, 20)
            ))
    
    # Full combo: persistence + opp suppress + execution
    for n in [5, 8]:
        for x_max in [15, 20]:
            configs.append(StrategyConfig(
                name=f"FULL_PERSIST{n}_OPPle{x_max}_pmax93_slip1",
                persist_n=n, opp_suppress=(x_max, 20), p_max=93, slip=1
            ))
    
    # Late + persistence combos
    for late in [60, 100]:
        for n in [3, 5]:
            configs.append(StrategyConfig(
                name=f"LATE{late}_PERSIST{n}",
                late_filter=late, persist_n=n
            ))
            configs.append(StrategyConfig(
                name=f"LATE{late}_PERSIST{n}_pmax93_slip1",
                late_filter=late, persist_n=n, p_max=93, slip=1
            ))
    
    return configs


def main() -> int:
    parser = argparse.ArgumentParser(description="Reversal-Killer Strategy Pack")
    parser.add_argument('--input', '-i', required=True, help='Input path')
    parser.add_argument('--outdir', '-o', default='out_reversal_killer', help='Output directory')
    
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
    
    # Build trade contexts
    print("Building trade contexts...")
    contexts: List[TradeContext] = []
    for window_id, ticks, _ in windows:
        ctx = build_trade_context(window_id, ticks)
        if ctx:
            contexts.append(ctx)
    
    print(f"Built {len(contexts)} trade contexts (windows with 90c touch)")
    
    # Generate and evaluate strategies
    print("Generating strategy configurations...")
    configs = generate_strategy_configs()
    print(f"Testing {len(configs)} strategy variants...")
    
    results: List[StrategyResult] = []
    for i, config in enumerate(configs):
        if (i + 1) % 20 == 0:
            print(f"  Evaluated {i+1}/{len(configs)} strategies...")
        result = evaluate_strategy(contexts, config)
        results.append(result)
    
    print(f"Completed evaluation of {len(results)} strategies")
    
    # Sort by EV/invested
    results.sort(key=lambda r: r.ev_invested, reverse=True)
    
    # Print summary
    print("\n" + "=" * 140)
    print("TOP 20 STRATEGIES BY EV/INVESTED")
    print("=" * 140)
    print(f"{'Rank':>4} {'Strategy':<45} {'Trades':>7} {'Win%':>8} {'Rev%':>7} {'AvgEnt':>8} "
          f"{'EV/sh':>9} {'EV/inv':>9} {'TotPnL':>10}")
    print("-" * 140)
    
    for i, r in enumerate(results[:20]):
        print(f"{i+1:>4} {r.name:<45} {r.trades:>7} {r.win_rate:>7.2%} {r.reversal_rate:>6.2%} "
              f"{r.avg_entry:>7.1f}c {r.ev_per_share:>+8.2%} {r.ev_invested:>+8.2%} {r.total_pnl:>+10.2f}")
    
    # Find baseline
    baseline = next((r for r in results if r.name == "BASELINE"), None)
    
    # Find best practical strategy (trades >= 500, reversal < 8%)
    practical = [r for r in results if r.trades >= 500 and r.reversal_rate < 0.08 and r.ev_invested > 0.005]
    practical.sort(key=lambda r: r.ev_invested, reverse=True)
    
    print("\n" + "=" * 140)
    print("BEST PRACTICAL STRATEGIES (trades >= 500, reversal% < 8%, EV > 0.5%)")
    print("=" * 140)
    print(f"{'Rank':>4} {'Strategy':<45} {'Trades':>7} {'Win%':>8} {'Rev%':>7} {'AvgEnt':>8} "
          f"{'EV/sh':>9} {'EV/inv':>9}")
    print("-" * 140)
    
    for i, r in enumerate(practical[:15]):
        print(f"{i+1:>4} {r.name:<45} {r.trades:>7} {r.win_rate:>7.2%} {r.reversal_rate:>6.2%} "
              f"{r.avg_entry:>7.1f}c {r.ev_per_share:>+8.2%} {r.ev_invested:>+8.2%}")
    
    # Slippage sensitivity for top strategies
    print("\n" + "=" * 100)
    print("SLIPPAGE SENSITIVITY FOR TOP STRATEGIES")
    print("=" * 100)
    
    top_base_strategies = ['PERSIST_5s', 'PERSIST_8s', 'OPP_le15_in20s', 'COMBO_PERSIST5_OPPle15']
    
    print(f"{'Strategy':<35} {'Slip':>5} {'Trades':>7} {'Win%':>8} {'AvgEnt':>8} {'EV/sh':>9} {'EV/inv':>9}")
    print("-" * 100)
    
    for base_name in top_base_strategies:
        for slip in [0, 1, 2, 3]:
            # Find or create config
            config = None
            for c in configs:
                if base_name in c.name and f'slip{slip}' in c.name:
                    config = c
                    break
            
            if config is None:
                # Create new config
                if 'PERSIST_5s' in base_name and 'OPP' not in base_name:
                    config = StrategyConfig(name=f"{base_name}_slip{slip}", persist_n=5, slip=slip, p_max=93)
                elif 'PERSIST_8s' in base_name:
                    config = StrategyConfig(name=f"{base_name}_slip{slip}", persist_n=8, slip=slip, p_max=93)
                elif 'OPP_le15' in base_name:
                    config = StrategyConfig(name=f"{base_name}_slip{slip}", opp_suppress=(15, 20), slip=slip, p_max=93)
                elif 'COMBO_PERSIST5_OPPle15' in base_name:
                    config = StrategyConfig(name=f"{base_name}_slip{slip}", persist_n=5, opp_suppress=(15, 20), slip=slip, p_max=93)
                else:
                    continue
                
                r = evaluate_strategy(contexts, config)
            else:
                r = next((res for res in results if res.name == config.name), None)
                if r is None:
                    r = evaluate_strategy(contexts, config)
            
            if r and r.trades > 0:
                print(f"{base_name:<35} {slip:>4}c {r.trades:>7} {r.win_rate:>7.2%} "
                      f"{r.avg_entry:>7.1f}c {r.ev_per_share:>+8.2%} {r.ev_invested:>+8.2%}")
    
    # Reversal reduction report
    print("\n" + "=" * 100)
    print("REVERSAL REDUCTION REPORT")
    print("=" * 100)
    
    if baseline:
        print(f"\nBaseline reversal%: {baseline.reversal_rate:.2%}")
        print(f"Baseline win%: {baseline.win_rate:.2%}")
        print(f"Baseline trades: {baseline.trades}")
        
        # Find best reversal reducers
        reducers = [(r.name, baseline.reversal_rate - r.reversal_rate, r.reversal_rate, r.trades) 
                    for r in results if r.trades >= 500]
        reducers.sort(key=lambda x: x[1], reverse=True)
        
        print(f"\nTop reversal reducers (trades >= 500):")
        print(f"{'Strategy':<45} {'Rev% Reduction':>15} {'Final Rev%':>12} {'Trades':>8}")
        print("-" * 85)
        for name, reduction, final, trades in reducers[:10]:
            print(f"{name:<45} {reduction:>+14.2%} {final:>11.2%} {trades:>8}")
    
    # Write outputs
    # strategies_summary.csv
    with open(outdir / 'strategies_summary.csv', 'w', newline='') as f:
        writer = csv.writer(f)
        writer.writerow(['name', 'trades', 'wins', 'reversals', 'win_rate', 'reversal_rate',
                        'avg_entry', 'avg_exit', 'ev_per_share', 'ev_invested', 'total_pnl', 'worst_loss'])
        for r in results:
            writer.writerow([r.name, r.trades, r.wins, r.reversals, r.win_rate, r.reversal_rate,
                            r.avg_entry, r.avg_exit, r.ev_per_share, r.ev_invested, r.total_pnl, r.worst_loss])
    
    # best_strategies.json
    best = [r for r in results if r.trades >= 500][:20]
    best_data = [{
        'name': r.name,
        'params': r.params,
        'trades': r.trades,
        'win_rate': r.win_rate,
        'reversal_rate': r.reversal_rate,
        'avg_entry': r.avg_entry,
        'ev_per_share': r.ev_per_share,
        'ev_invested': r.ev_invested,
    } for r in best]
    
    with open(outdir / 'best_strategies.json', 'w') as f:
        json.dump(best_data, f, indent=2)
    
    # Recommended deploy rule
    print("\n" + "=" * 100)
    print("RECOMMENDED DEPLOY RULE")
    print("=" * 100)
    
    # Find best under conservative execution
    conservative = [r for r in results 
                   if 'pmax93_slip1' in r.name 
                   and r.trades >= 500 
                   and r.reversal_rate < 0.078
                   and r.ev_invested > 0.005]
    conservative.sort(key=lambda r: r.ev_invested, reverse=True)
    
    if conservative:
        rec = conservative[0]
        print(f"""
DEPLOY RULE: {rec.name}

Parameters:
  - Trigger: First touch >= 90c
  - Persistence: {rec.params.get('persist_n', 'N/A')}s
  - Opp Suppression: {rec.params.get('opp_suppress', 'N/A')}
  - Execution: p_max=93c, slip=+1c

Expected Performance (conservative):
  - Trades: {rec.trades}
  - Win Rate: {rec.win_rate:.2%}
  - Reversal Rate: {rec.reversal_rate:.2%}
  - Avg Entry: {rec.avg_entry:.1f}c
  - EV/share: {rec.ev_per_share:+.2%}
  - EV/invested: {rec.ev_invested:+.2%}

Execution Logic:
  1. When UP or DOWN >= 90c, start confirmation timer
  2. Wait {rec.params.get('persist_n', 5)}s, confirm side stays >= 90c
  3. If opp_suppress set, verify opposite <= threshold
  4. Place LIMIT BUY at 93c
  5. If not filled in 2 seconds, cancel
  6. Hold to settlement (no early exit in this config)
  7. Position size: 3% bankroll (1/4 Kelly)
""")
    else:
        print("No strategy meets conservative criteria. Review results manually.")
    
    print(f"\nResults written to: {outdir}/")
    print(f"  - strategies_summary.csv")
    print(f"  - best_strategies.json")
    
    return 0


if __name__ == '__main__':
    sys.exit(main())

