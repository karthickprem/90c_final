#!/usr/bin/env python3
"""
Reversal-Killer Strategy Pack V2

Adds post-entry risk controls:
- TP: Take profit when side >= TP
- SL: Stop loss when side <= SL  
- OPP_KILL: Exit when opposite >= X within T seconds

Risk-adjusted metrics:
- EV/invested
- worst_loss_per_trade
- 99th percentile loss
- max drawdown (simulated bankroll)
- profit factor
"""

from __future__ import annotations

import argparse
import csv
import json
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import List, Dict, Any, Optional, Tuple
import math

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
    first_touch_side: str
    trigger_price: int
    winner: str
    max_time: float
    pre_ticks: List[Tick] = field(default_factory=list)
    post_ticks: List[Tick] = field(default_factory=list)


@dataclass 
class TradeResult:
    """Result of a single trade."""
    window_id: str
    first_touch_side: str
    trigger_price: int
    fill_price: int
    exit_price: int
    exit_reason: str
    won: bool
    pnl: float
    secs_left: float
    is_reversal: bool
    time_held: float = 0


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
    pct_99_loss: float
    max_drawdown: float
    profit_factor: float
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
    
    for t in reversed(sorted_ticks):
        up, down = t.up_cents / 100, t.down_cents / 100
        if max(up, down) >= 0.97 and min(up, down) <= 0.03:
            return ('UP' if up > down else 'DOWN'), t.elapsed_seconds
    
    last = sorted_ticks[-1]
    up, down = last.up_cents / 100, last.down_cents / 100
    if abs(up - down) < 0.05 and max(up, down) < 0.60:
        return 'UNCLEAR', None
    return ('UP' if up > down else 'DOWN'), last.elapsed_seconds


def get_price(tick: Tick, side: str) -> int:
    return tick.up_cents if side == 'UP' else tick.down_cents


def get_opp_price(tick: Tick, side: str) -> int:
    return tick.down_cents if side == 'UP' else tick.up_cents


def build_trade_context(window_id: str, ticks: List[Tick], threshold: int = 90) -> Optional[TradeContext]:
    """Build trade context for a window."""
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
        up_touched = t.up_cents >= threshold
        down_touched = t.down_cents >= threshold
        
        if up_touched and down_touched:
            return None  # TIE - skip
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
# Entry Filters
# ============================================================================

def check_persistence(ctx: TradeContext, n_secs: float, threshold: int = 90) -> bool:
    end_time = ctx.trigger_time + n_secs
    for t in ctx.post_ticks:
        if t.elapsed_seconds <= end_time:
            if get_price(t, ctx.first_touch_side) < threshold:
                return False
    return True


def check_spike_vs_sticky(ctx: TradeContext, floor: int, push: int, m_secs: float) -> bool:
    end_time = ctx.trigger_time + m_secs
    prices = [get_price(t, ctx.first_touch_side) 
              for t in ctx.post_ticks if t.elapsed_seconds <= end_time]
    
    if not prices:
        return ctx.trigger_price >= floor and ctx.trigger_price >= push
    
    return min(prices) >= floor and max(prices) >= push


def check_opp_suppression(ctx: TradeContext, x_max: int, m_secs: float) -> bool:
    end_time = ctx.trigger_time + m_secs
    for t in ctx.post_ticks:
        if t.elapsed_seconds <= end_time:
            if get_opp_price(t, ctx.first_touch_side) > x_max:
                return False
    return True


def get_secs_left(ctx: TradeContext) -> float:
    return max(0, 900 - ctx.trigger_time) if ctx.max_time < 850 else max(0, ctx.max_time - ctx.trigger_time)


# ============================================================================
# Post-Entry Exit Simulation (with slippage)
# ============================================================================

def simulate_exit_v2(
    ctx: TradeContext,
    fill_price: int,
    entry_time: float,
    slip_exit: int = 1,  # Slippage against you on exit
    stop_loss: Optional[int] = None,
    stop_loss_window: Optional[float] = None,  # SL only active for first N seconds
    take_profit: Optional[int] = None,
    opp_kill: Optional[Tuple[int, float]] = None,  # (threshold, max_secs)
) -> Tuple[int, str, float]:
    """
    Simulate post-entry exit with slippage.
    
    Returns: (exit_price, exit_reason, time_held)
    
    Exit price includes slippage against you:
    - For early exits (SL, OPP_KILL): exit at tick_price - slip_exit (you're selling)
    - For TP: exit at TP (limit sell, no slip)
    - For settlement: 100 if won, 0 if lost
    """
    side = ctx.first_touch_side
    
    for t in ctx.post_ticks:
        time_since_entry = t.elapsed_seconds - entry_time
        self_price = get_price(t, side)
        opp_price = get_opp_price(t, side)
        
        # Check take-profit first (best outcome)
        if take_profit and self_price >= take_profit:
            exit_price = take_profit  # Limit sell at TP, no slip
            return exit_price, 'TAKE_PROFIT', time_since_entry
        
        # Check stop-loss (may have time window)
        if stop_loss:
            sl_active = True
            if stop_loss_window and time_since_entry > stop_loss_window:
                sl_active = False
            if sl_active and self_price <= stop_loss:
                exit_price = max(0, self_price - slip_exit)  # Sell with slippage
                return exit_price, 'STOP_LOSS', time_since_entry
        
        # Check opp_kill
        if opp_kill:
            opp_threshold, opp_max_secs = opp_kill
            if time_since_entry <= opp_max_secs:
                if opp_price >= opp_threshold:
                    exit_price = max(0, self_price - slip_exit)
                    return exit_price, 'OPP_KILL', time_since_entry
    
    # Held to settlement
    time_held = ctx.max_time - entry_time if ctx.post_ticks else 0
    if ctx.winner == side:
        return 100, 'SETTLEMENT_WIN', time_held
    else:
        return 0, 'SETTLEMENT_LOSS', time_held


# ============================================================================
# Strategy Configuration
# ============================================================================

@dataclass
class StrategyConfig:
    """Configuration for a strategy variant."""
    name: str
    
    # Entry filters
    persist_n: Optional[int] = None
    spike_sticky: Optional[Tuple[int, int, float]] = None  # (floor, push, m)
    opp_suppress: Optional[Tuple[int, float]] = None
    late_filter: Optional[float] = None
    
    # Execution
    p_max: Optional[int] = None
    slip_entry: int = 0
    slip_exit: int = 0
    
    # Exit rules
    stop_loss: Optional[int] = None
    stop_loss_window: Optional[float] = None
    take_profit: Optional[int] = None
    opp_kill: Optional[Tuple[int, float]] = None  # (threshold, max_secs)


def evaluate_strategy(
    contexts: List[TradeContext],
    config: StrategyConfig
) -> StrategyResult:
    """Evaluate a strategy with full risk metrics."""
    
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
        
        if config.late_filter:
            if get_secs_left(ctx) > config.late_filter:
                continue
        
        # Compute fill price with entry slippage
        trigger_price = ctx.trigger_price
        fill_price = trigger_price + config.slip_entry
        
        if config.p_max is not None:
            if trigger_price > config.p_max:
                continue  # Skip trade
            fill_price = min(fill_price, config.p_max)
        
        # Determine entry time (after SPIKE wait if applicable)
        if config.spike_sticky:
            _, _, m = config.spike_sticky
            entry_time = ctx.trigger_time + m
        elif config.persist_n:
            entry_time = ctx.trigger_time + config.persist_n
        else:
            entry_time = ctx.trigger_time
        
        # Simulate exit
        exit_price, exit_reason, time_held = simulate_exit_v2(
            ctx,
            fill_price,
            entry_time,
            slip_exit=config.slip_exit,
            stop_loss=config.stop_loss,
            stop_loss_window=config.stop_loss_window,
            take_profit=config.take_profit,
            opp_kill=config.opp_kill,
        )
        
        # Calculate PnL per share
        entry_frac = fill_price / 100
        exit_frac = exit_price / 100
        pnl = exit_frac - entry_frac
        won = pnl > 0
        
        is_reversal = ctx.first_touch_side != ctx.winner
        
        trade_results.append(TradeResult(
            window_id=ctx.window_id,
            first_touch_side=ctx.first_touch_side,
            trigger_price=ctx.trigger_price,
            fill_price=fill_price,
            exit_price=exit_price,
            exit_reason=exit_reason,
            won=won,
            pnl=pnl,
            secs_left=get_secs_left(ctx),
            is_reversal=is_reversal,
            time_held=time_held,
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
            pct_99_loss=0, max_drawdown=0,
            profit_factor=0,
        )
    
    wins = sum(1 for t in trade_results if t.won)
    reversals = sum(1 for t in trade_results if t.is_reversal)
    avg_entry = sum(t.fill_price for t in trade_results) / n
    avg_exit = sum(t.exit_price for t in trade_results) / n
    
    win_rate = wins / n
    reversal_rate = reversals / n
    
    p = avg_entry / 100
    ev_per_share = sum(t.pnl for t in trade_results) / n
    ev_invested = ev_per_share / p if p > 0 else 0
    
    total_pnl = sum(t.pnl for t in trade_results)
    
    # Risk metrics
    losses = sorted([t.pnl for t in trade_results if t.pnl < 0])
    worst_loss = min(losses) if losses else 0
    pct_99_loss = losses[int(len(losses) * 0.01)] if len(losses) > 100 else worst_loss
    
    # Profit factor
    gross_profit = sum(t.pnl for t in trade_results if t.pnl > 0)
    gross_loss = abs(sum(t.pnl for t in trade_results if t.pnl < 0))
    profit_factor = gross_profit / gross_loss if gross_loss > 0 else float('inf')
    
    # Max drawdown simulation (f = 3% bankroll per trade)
    f = 0.03
    bankroll = 1.0
    peak = 1.0
    max_dd = 0.0
    
    for t in trade_results:
        # Return on this trade
        if t.pnl > 0:
            trade_return = f * (t.pnl / (t.fill_price / 100))
        else:
            trade_return = f * (t.pnl / (t.fill_price / 100))
        
        bankroll *= (1 + trade_return)
        peak = max(peak, bankroll)
        dd = (peak - bankroll) / peak
        max_dd = max(max_dd, dd)
    
    return StrategyResult(
        name=config.name,
        params={
            'persist_n': config.persist_n,
            'spike_sticky': config.spike_sticky,
            'opp_suppress': config.opp_suppress,
            'late_filter': config.late_filter,
            'stop_loss': config.stop_loss,
            'stop_loss_window': config.stop_loss_window,
            'take_profit': config.take_profit,
            'opp_kill': config.opp_kill,
            'p_max': config.p_max,
            'slip_entry': config.slip_entry,
            'slip_exit': config.slip_exit,
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
        pct_99_loss=round(pct_99_loss, 4),
        max_drawdown=round(max_dd, 4),
        profit_factor=round(profit_factor, 2),
        trade_results=trade_results,
    )


# ============================================================================
# Strategy Generation
# ============================================================================

def generate_strategy_configs() -> List[StrategyConfig]:
    """Generate all strategy configurations to test."""
    configs = []
    
    # Baseline
    configs.append(StrategyConfig(name="BASELINE"))
    
    # SPIKE filter variations
    for floor in [87, 88, 89]:
        for push in [92, 93, 94]:
            for m in [10]:
                name = f"SPIKE_f{floor}_p{push}_m{m}"
                configs.append(StrategyConfig(name=name, spike_sticky=(floor, push, m)))
    
    # SPIKE + execution
    configs.append(StrategyConfig(
        name="SPIKE_f88_p93_m10_exec",
        spike_sticky=(88, 93, 10), p_max=93, slip_entry=1, slip_exit=1
    ))
    
    # SPIKE + TP variations
    for tp in [95, 97, 99]:
        configs.append(StrategyConfig(
            name=f"SPIKE_f88_p93_m10_TP{tp}",
            spike_sticky=(88, 93, 10), take_profit=tp
        ))
    
    # SPIKE + SL variations
    for sl in [84, 86, 88]:
        configs.append(StrategyConfig(
            name=f"SPIKE_f88_p93_m10_SL{sl}",
            spike_sticky=(88, 93, 10), stop_loss=sl
        ))
        # SL with time window
        configs.append(StrategyConfig(
            name=f"SPIKE_f88_p93_m10_SL{sl}_60s",
            spike_sticky=(88, 93, 10), stop_loss=sl, stop_loss_window=60
        ))
    
    # SPIKE + OPP_KILL variations
    for x in [20, 25, 30]:
        for t in [20, 30, 45]:
            configs.append(StrategyConfig(
                name=f"SPIKE_f88_p93_m10_OPPKILL{x}_{t}s",
                spike_sticky=(88, 93, 10), opp_kill=(x, t)
            ))
    
    # Full combo: SPIKE + TP + SL + OPP_KILL + execution
    configs.append(StrategyConfig(
        name="SPIKE_FULL_v1",
        spike_sticky=(88, 93, 10),
        p_max=93, slip_entry=1, slip_exit=1,
        take_profit=97,
        stop_loss=86, stop_loss_window=60,
        opp_kill=(25, 30),
    ))
    
    configs.append(StrategyConfig(
        name="SPIKE_FULL_v2",
        spike_sticky=(88, 93, 10),
        p_max=93, slip_entry=1, slip_exit=1,
        take_profit=97,
        stop_loss=84, stop_loss_window=60,
        opp_kill=(25, 30),
    ))
    
    configs.append(StrategyConfig(
        name="SPIKE_FULL_v3_aggressive",
        spike_sticky=(88, 93, 10),
        p_max=93, slip_entry=1, slip_exit=1,
        take_profit=95,
        stop_loss=88, stop_loss_window=30,
        opp_kill=(20, 20),
    ))
    
    configs.append(StrategyConfig(
        name="SPIKE_FULL_v4_conservative",
        spike_sticky=(89, 94, 10),
        p_max=93, slip_entry=1, slip_exit=1,
        take_profit=97,
        stop_loss=86, stop_loss_window=60,
        opp_kill=(25, 30),
    ))
    
    # PERSIST variations with exits
    for n in [5, 8]:
        configs.append(StrategyConfig(
            name=f"PERSIST{n}_exec",
            persist_n=n, p_max=93, slip_entry=1, slip_exit=1
        ))
        configs.append(StrategyConfig(
            name=f"PERSIST{n}_OPPKILL25_30s",
            persist_n=n, opp_kill=(25, 30)
        ))
        configs.append(StrategyConfig(
            name=f"PERSIST{n}_SL86_TP97",
            persist_n=n, stop_loss=86, take_profit=97
        ))
        configs.append(StrategyConfig(
            name=f"PERSIST{n}_FULL",
            persist_n=n, p_max=93, slip_entry=1, slip_exit=1,
            stop_loss=86, take_profit=97, opp_kill=(25, 30)
        ))
    
    # LATE + PERSIST with exits
    for late in [60, 100]:
        for n in [3, 5]:
            configs.append(StrategyConfig(
                name=f"LATE{late}_PERSIST{n}_FULL",
                late_filter=late, persist_n=n,
                p_max=93, slip_entry=1, slip_exit=1,
                stop_loss=86, take_profit=97
            ))
    
    return configs


def main() -> int:
    parser = argparse.ArgumentParser(description="Reversal-Killer V2 with Risk Controls")
    parser.add_argument('--input', '-i', required=True, help='Input path')
    parser.add_argument('--outdir', '-o', default='out_reversal_killer_v2', help='Output directory')
    
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
    
    print("Generating strategy configurations...")
    configs = generate_strategy_configs()
    print(f"Testing {len(configs)} strategy variants...")
    
    results: List[StrategyResult] = []
    for i, config in enumerate(configs):
        if (i + 1) % 10 == 0:
            print(f"  Evaluated {i+1}/{len(configs)} strategies...")
        result = evaluate_strategy(contexts, config)
        results.append(result)
    
    print(f"Completed evaluation of {len(results)} strategies")
    
    # Sort by EV/invested
    results.sort(key=lambda r: r.ev_invested, reverse=True)
    
    # Find baseline
    baseline = next((r for r in results if r.name == "BASELINE"), None)
    
    # Print results
    print("\n" + "=" * 160)
    print("TOP 20 STRATEGIES BY EV/INVESTED (with risk metrics)")
    print("=" * 160)
    print(f"{'Rank':>4} {'Strategy':<40} {'Trades':>7} {'Win%':>7} {'Rev%':>6} {'AvgEnt':>7} "
          f"{'EV/sh':>8} {'EV/inv':>8} {'WorstL':>8} {'99%L':>7} {'MaxDD':>7} {'PF':>6}")
    print("-" * 160)
    
    for i, r in enumerate(results[:20]):
        print(f"{i+1:>4} {r.name:<40} {r.trades:>7} {r.win_rate:>6.1%} {r.reversal_rate:>5.1%} "
              f"{r.avg_entry:>6.1f}c {r.ev_per_share:>+7.2%} {r.ev_invested:>+7.2%} "
              f"{r.worst_loss:>+7.1%} {r.pct_99_loss:>+6.1%} {r.max_drawdown:>6.1%} {r.profit_factor:>6.2f}")
    
    # Exit reason breakdown for key strategies
    print("\n" + "=" * 100)
    print("EXIT REASON BREAKDOWN FOR KEY STRATEGIES")
    print("=" * 100)
    
    key_strategies = ['BASELINE', 'SPIKE_f88_p93_m10', 'SPIKE_FULL_v1', 'SPIKE_FULL_v2', 'PERSIST8_FULL']
    
    for name in key_strategies:
        r = next((res for res in results if res.name == name), None)
        if not r or not r.trade_results:
            continue
        
        exit_counts = {}
        for t in r.trade_results:
            exit_counts[t.exit_reason] = exit_counts.get(t.exit_reason, 0) + 1
        
        print(f"\n{name}:")
        for reason, count in sorted(exit_counts.items()):
            pct = count / r.trades * 100
            avg_pnl = sum(t.pnl for t in r.trade_results if t.exit_reason == reason) / count
            print(f"  {reason:<20} {count:>5} ({pct:>5.1f}%)  avg_pnl: {avg_pnl:>+6.2%}")
    
    # Compare with baseline
    print("\n" + "=" * 100)
    print("COMPARISON VS BASELINE")
    print("=" * 100)
    
    if baseline:
        print(f"\nBaseline: {baseline.trades} trades, Rev%={baseline.reversal_rate:.2%}, "
              f"EV/inv={baseline.ev_invested:+.2%}, WorstL={baseline.worst_loss:+.2%}, "
              f"MaxDD={baseline.max_drawdown:.2%}")
        
        print(f"\n{'Strategy':<40} {'dRev%':>8} {'dEV/inv':>10} {'dWorstL':>10} {'dMaxDD':>8}")
        print("-" * 80)
        
        for r in results[:15]:
            if r.name == 'BASELINE':
                continue
            d_rev = r.reversal_rate - baseline.reversal_rate
            d_ev = r.ev_invested - baseline.ev_invested
            d_worst = r.worst_loss - baseline.worst_loss
            d_dd = r.max_drawdown - baseline.max_drawdown
            print(f"{r.name:<40} {d_rev:>+7.2%} {d_ev:>+9.2%} {d_worst:>+9.2%} {d_dd:>+7.2%}")
    
    # Risk-adjusted ranking
    print("\n" + "=" * 100)
    print("RISK-ADJUSTED RANKING (EV/invested / MaxDD)")
    print("=" * 100)
    
    risk_adj = [(r, r.ev_invested / r.max_drawdown if r.max_drawdown > 0 else float('inf')) 
                for r in results if r.trades >= 500 and r.ev_invested > 0]
    risk_adj.sort(key=lambda x: x[1], reverse=True)
    
    print(f"{'Rank':>4} {'Strategy':<40} {'Trades':>7} {'EV/inv':>8} {'MaxDD':>7} {'Ratio':>8}")
    print("-" * 80)
    
    for i, (r, ratio) in enumerate(risk_adj[:15]):
        print(f"{i+1:>4} {r.name:<40} {r.trades:>7} {r.ev_invested:>+7.2%} "
              f"{r.max_drawdown:>6.2%} {ratio:>8.2f}")
    
    # The deployable combo test
    print("\n" + "=" * 120)
    print("DEPLOYABLE COMBO: SPIKE_f88_p93_m10 + pmax93 + slip1 + OPPKILL(25,30s) + SL86 + TP97")
    print("=" * 120)
    
    deploy = next((r for r in results if r.name == 'SPIKE_FULL_v1'), None)
    spike_base = next((r for r in results if r.name == 'SPIKE_f88_p93_m10'), None)
    
    if deploy and spike_base:
        print(f"\n{'Metric':<25} {'SPIKE Base':>15} {'SPIKE + Exits':>15} {'Delta':>12}")
        print("-" * 70)
        print(f"{'Trades':<25} {spike_base.trades:>15} {deploy.trades:>15} {deploy.trades - spike_base.trades:>+12}")
        print(f"{'Win Rate':<25} {spike_base.win_rate:>14.2%} {deploy.win_rate:>14.2%} {deploy.win_rate - spike_base.win_rate:>+11.2%}")
        print(f"{'Reversal Rate':<25} {spike_base.reversal_rate:>14.2%} {deploy.reversal_rate:>14.2%} {deploy.reversal_rate - spike_base.reversal_rate:>+11.2%}")
        print(f"{'EV/share':<25} {spike_base.ev_per_share:>+14.2%} {deploy.ev_per_share:>+14.2%} {deploy.ev_per_share - spike_base.ev_per_share:>+11.2%}")
        print(f"{'EV/invested':<25} {spike_base.ev_invested:>+14.2%} {deploy.ev_invested:>+14.2%} {deploy.ev_invested - spike_base.ev_invested:>+11.2%}")
        print(f"{'Worst Loss':<25} {spike_base.worst_loss:>+14.2%} {deploy.worst_loss:>+14.2%} {deploy.worst_loss - spike_base.worst_loss:>+11.2%}")
        print(f"{'99% Loss':<25} {spike_base.pct_99_loss:>+14.2%} {deploy.pct_99_loss:>+14.2%} {deploy.pct_99_loss - spike_base.pct_99_loss:>+11.2%}")
        print(f"{'Max Drawdown':<25} {spike_base.max_drawdown:>14.2%} {deploy.max_drawdown:>14.2%} {deploy.max_drawdown - spike_base.max_drawdown:>+11.2%}")
        print(f"{'Profit Factor':<25} {spike_base.profit_factor:>15.2f} {deploy.profit_factor:>15.2f} {deploy.profit_factor - spike_base.profit_factor:>+12.2f}")
    
    # Final recommendation
    print("\n" + "=" * 100)
    print("FINAL DEPLOYMENT RECOMMENDATION")
    print("=" * 100)
    
    # Find best practical with exits
    practical = [r for r in results 
                 if r.trades >= 500 
                 and r.reversal_rate < 0.06
                 and r.ev_invested > 0.02
                 and 'FULL' in r.name or 'OPPKILL' in r.name]
    practical.sort(key=lambda r: r.ev_invested, reverse=True)
    
    if practical:
        rec = practical[0]
        print(f"""
RECOMMENDED: {rec.name}

Entry:
  - Trigger: First touch >= 90c
  - Wait 10 seconds
  - Validate: min >= 88c AND max >= 93c in those 10s
  - Execute: LIMIT BUY at 93c, slip_entry = +1c

Exits:
  - TAKE PROFIT: Exit when side >= 97c
  - STOP LOSS: Exit when side <= 86c (within first 60s)
  - OPP KILL: Exit when opposite >= 25c (within first 30s)
  - slip_exit = +1c against you

Performance (conservative):
  - Trades: {rec.trades}
  - Win Rate: {rec.win_rate:.2%}
  - Reversal Rate: {rec.reversal_rate:.2%}
  - EV/share: {rec.ev_per_share:+.2%}
  - EV/invested: {rec.ev_invested:+.2%}
  - Worst Loss: {rec.worst_loss:+.2%}
  - 99th pct Loss: {rec.pct_99_loss:+.2%}
  - Max Drawdown (@3% size): {rec.max_drawdown:.2%}
  - Profit Factor: {rec.profit_factor:.2f}

Position Sizing:
  - Start: 2% bankroll per trade
  - After 300+ trades with verified stats: increase to 3%
  - Never exceed 5%
""")
    
    # Write outputs
    with open(outdir / 'strategies_summary.csv', 'w', newline='') as f:
        writer = csv.writer(f)
        writer.writerow(['name', 'trades', 'wins', 'reversals', 'win_rate', 'reversal_rate',
                        'avg_entry', 'avg_exit', 'ev_per_share', 'ev_invested', 'total_pnl',
                        'worst_loss', 'pct_99_loss', 'max_drawdown', 'profit_factor'])
        for r in results:
            writer.writerow([r.name, r.trades, r.wins, r.reversals, r.win_rate, r.reversal_rate,
                            r.avg_entry, r.avg_exit, r.ev_per_share, r.ev_invested, r.total_pnl,
                            r.worst_loss, r.pct_99_loss, r.max_drawdown, r.profit_factor])
    
    with open(outdir / 'best_strategies.json', 'w') as f:
        best_data = [{
            'name': r.name,
            'params': r.params,
            'trades': r.trades,
            'win_rate': r.win_rate,
            'reversal_rate': r.reversal_rate,
            'ev_per_share': r.ev_per_share,
            'ev_invested': r.ev_invested,
            'worst_loss': r.worst_loss,
            'max_drawdown': r.max_drawdown,
            'profit_factor': r.profit_factor,
        } for r in results[:20]]
        json.dump(best_data, f, indent=2)
    
    print(f"\nResults written to: {outdir}/")
    
    return 0


if __name__ == '__main__':
    sys.exit(main())


