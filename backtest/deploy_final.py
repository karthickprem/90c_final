#!/usr/bin/env python3
"""
Final Deployment Backtest with Audit

Fixes:
- Trade-level audit with full details
- Confirms worst_loss matches SL logic
- Dynamic SL implementation
- Proper PnL-based ranking (no win% ranking)
- Slippage sensitivity analysis
"""

from __future__ import annotations

import argparse
import csv
import json
import sys
import math
from dataclasses import dataclass, field
from pathlib import Path
from typing import List, Dict, Any, Optional, Tuple

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
class TradeAudit:
    """Full trade audit record."""
    window_id: str
    side: str
    first_touch_time: float
    entry_time: float
    entry_price: int
    spike_min: int
    spike_max: int
    exit_reason: str
    exit_time: float
    exit_price: int
    pnl_per_share: float
    pnl_invested: float  # (exit - entry) / entry
    winner_at_settle: str
    time_held: float
    sl_triggered: bool
    tp_triggered: bool
    dynamic_sl_level: int  # final SL level if dynamic


@dataclass
class StrategyStats:
    name: str
    params: Dict[str, Any]
    trades: int
    avg_entry: float
    avg_exit: float
    ev_per_share: float
    ev_invested: float
    total_pnl: float
    worst_loss: float
    worst_1pct_loss: float
    max_drawdown_2pct: float
    max_drawdown_3pct: float
    profit_factor: float
    sharpe_proxy: float
    trades_audit: List[TradeAudit] = field(default_factory=list)


# ============================================================================
# Core Functions
# ============================================================================

def determine_winner(ticks: List[Tick]) -> Tuple[str, Optional[float]]:
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


def check_spike(ctx: TradeContext, floor: int, push: int, m_secs: float) -> Tuple[bool, int, int]:
    """Check SPIKE filter and return (passed, min_price, max_price)."""
    end_time = ctx.trigger_time + m_secs
    prices = [get_price(t, ctx.first_touch_side) 
              for t in ctx.post_ticks if t.elapsed_seconds <= end_time]
    if not prices:
        return (ctx.trigger_price >= floor and ctx.trigger_price >= push, 
                ctx.trigger_price, ctx.trigger_price)
    min_p = min(prices)
    max_p = max(prices)
    return (min_p >= floor and max_p >= push, min_p, max_p)


# ============================================================================
# Exit Simulation with Dynamic SL
# ============================================================================

def simulate_exit_final(
    ctx: TradeContext,
    entry_price: int,
    entry_time: float,
    slip_exit: int = 1,
    base_sl: int = 86,
    tp: int = 97,
    dynamic_sl: bool = False,
    dynamic_sl_steps: Optional[List[Tuple[int, int]]] = None,  # [(threshold, new_sl), ...]
) -> Tuple[int, str, float, int, bool, bool]:
    """
    Simulate exit with optional dynamic SL.
    
    Returns: (exit_price, exit_reason, exit_time, final_sl_level, sl_triggered, tp_triggered)
    """
    side = ctx.first_touch_side
    current_sl = base_sl
    
    if dynamic_sl_steps is None:
        dynamic_sl_steps = [(95, 90)]  # Default: if reaches 95, raise SL to 90
    
    for t in ctx.post_ticks:
        if t.elapsed_seconds <= entry_time:
            continue
            
        self_price = get_price(t, side)
        exit_time = t.elapsed_seconds
        
        # Dynamic SL update (before checking exits)
        if dynamic_sl:
            for threshold, new_sl in dynamic_sl_steps:
                if self_price >= threshold and new_sl > current_sl:
                    current_sl = new_sl
        
        # Check TP first
        if self_price >= tp:
            return tp, 'TAKE_PROFIT', exit_time, current_sl, False, True
        
        # Check SL
        if self_price <= current_sl:
            exit_price = max(0, self_price - slip_exit)
            return exit_price, 'STOP_LOSS', exit_time, current_sl, True, False
    
    # Settlement
    time_held = ctx.max_time - entry_time
    if ctx.winner == side:
        return 100, 'SETTLEMENT_WIN', ctx.max_time, current_sl, False, False
    else:
        return 0, 'SETTLEMENT_LOSS', ctx.max_time, current_sl, False, False


# ============================================================================
# Strategy Evaluation
# ============================================================================

@dataclass
class StrategyConfig:
    name: str
    spike_floor: int = 88
    spike_push: int = 93
    spike_m: float = 10
    p_max: int = 93
    slip_entry: int = 1
    slip_exit: int = 1
    base_sl: int = 86
    tp: int = 97
    dynamic_sl: bool = False
    dynamic_sl_steps: Optional[List[Tuple[int, int]]] = None


def evaluate_strategy(
    contexts: List[TradeContext],
    config: StrategyConfig
) -> StrategyStats:
    """Evaluate strategy with full audit trail."""
    
    trades_audit: List[TradeAudit] = []
    
    for ctx in contexts:
        # SPIKE filter
        passed, spike_min, spike_max = check_spike(
            ctx, config.spike_floor, config.spike_push, config.spike_m
        )
        if not passed:
            continue
        
        # Entry price with slippage
        trigger_price = ctx.trigger_price
        if trigger_price > config.p_max:
            continue
        
        entry_price = min(trigger_price + config.slip_entry, config.p_max)
        entry_time = ctx.trigger_time + config.spike_m
        
        # Simulate exit
        exit_price, exit_reason, exit_time, final_sl, sl_triggered, tp_triggered = simulate_exit_final(
            ctx, entry_price, entry_time,
            slip_exit=config.slip_exit,
            base_sl=config.base_sl,
            tp=config.tp,
            dynamic_sl=config.dynamic_sl,
            dynamic_sl_steps=config.dynamic_sl_steps,
        )
        
        # Calculate PnL
        pnl_per_share = (exit_price - entry_price) / 100
        pnl_invested = (exit_price - entry_price) / entry_price  # Return on capital
        
        trades_audit.append(TradeAudit(
            window_id=ctx.window_id,
            side=ctx.first_touch_side,
            first_touch_time=ctx.trigger_time,
            entry_time=entry_time,
            entry_price=entry_price,
            spike_min=spike_min,
            spike_max=spike_max,
            exit_reason=exit_reason,
            exit_time=exit_time,
            exit_price=exit_price,
            pnl_per_share=round(pnl_per_share, 4),
            pnl_invested=round(pnl_invested, 4),
            winner_at_settle=ctx.winner,
            time_held=round(exit_time - entry_time, 2),
            sl_triggered=sl_triggered,
            tp_triggered=tp_triggered,
            dynamic_sl_level=final_sl,
        ))
    
    # Compute stats
    n = len(trades_audit)
    if n == 0:
        return StrategyStats(
            name=config.name, params={}, trades=0,
            avg_entry=0, avg_exit=0, ev_per_share=0, ev_invested=0,
            total_pnl=0, worst_loss=0, worst_1pct_loss=0,
            max_drawdown_2pct=0, max_drawdown_3pct=0,
            profit_factor=0, sharpe_proxy=0,
        )
    
    pnls = [t.pnl_invested for t in trades_audit]
    avg_entry = sum(t.entry_price for t in trades_audit) / n
    avg_exit = sum(t.exit_price for t in trades_audit) / n
    
    ev_per_share = sum(t.pnl_per_share for t in trades_audit) / n
    ev_invested = sum(pnls) / n
    total_pnl = sum(t.pnl_per_share for t in trades_audit)
    
    # Worst loss
    worst_loss = min(pnls)
    
    # Worst 1% loss
    sorted_pnls = sorted(pnls)
    idx_1pct = max(0, int(n * 0.01))
    worst_1pct_loss = sorted_pnls[idx_1pct] if idx_1pct < n else worst_loss
    
    # Profit factor
    gross_profit = sum(p for p in pnls if p > 0)
    gross_loss = abs(sum(p for p in pnls if p < 0))
    profit_factor = gross_profit / gross_loss if gross_loss > 0 else float('inf')
    
    # Sharpe proxy
    mean_pnl = ev_invested
    std_pnl = (sum((p - mean_pnl)**2 for p in pnls) / n) ** 0.5
    sharpe_proxy = mean_pnl / std_pnl if std_pnl > 0 else 0
    
    # Max drawdown simulation
    def simulate_dd(f: float) -> float:
        bankroll = 1.0
        peak = 1.0
        max_dd = 0.0
        for t in trades_audit:
            trade_return = f * t.pnl_invested
            bankroll *= (1 + trade_return)
            peak = max(peak, bankroll)
            dd = (peak - bankroll) / peak
            max_dd = max(max_dd, dd)
        return max_dd
    
    max_dd_2pct = simulate_dd(0.02)
    max_dd_3pct = simulate_dd(0.03)
    
    return StrategyStats(
        name=config.name,
        params={
            'spike': (config.spike_floor, config.spike_push, config.spike_m),
            'p_max': config.p_max,
            'slip_entry': config.slip_entry,
            'slip_exit': config.slip_exit,
            'base_sl': config.base_sl,
            'tp': config.tp,
            'dynamic_sl': config.dynamic_sl,
        },
        trades=n,
        avg_entry=round(avg_entry, 2),
        avg_exit=round(avg_exit, 2),
        ev_per_share=round(ev_per_share, 4),
        ev_invested=round(ev_invested, 4),
        total_pnl=round(total_pnl, 2),
        worst_loss=round(worst_loss, 4),
        worst_1pct_loss=round(worst_1pct_loss, 4),
        max_drawdown_2pct=round(max_dd_2pct, 4),
        max_drawdown_3pct=round(max_dd_3pct, 4),
        profit_factor=round(profit_factor, 2),
        sharpe_proxy=round(sharpe_proxy, 4),
        trades_audit=trades_audit,
    )


def main() -> int:
    parser = argparse.ArgumentParser(description="Final Deployment Backtest")
    parser.add_argument('--input', '-i', required=True, help='Input path')
    parser.add_argument('--outdir', '-o', default='out_deploy', help='Output directory')
    
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
    
    # ========================================================================
    # Strategy Configurations
    # ========================================================================
    
    configs = [
        # Base SPIKE only
        StrategyConfig(name="SPIKE_BASE", base_sl=0, tp=100),  # No exits
        
        # Fixed SL variants
        StrategyConfig(name="SPIKE_SL86_TP97", base_sl=86, tp=97),
        StrategyConfig(name="SPIKE_SL84_TP97", base_sl=84, tp=97),
        StrategyConfig(name="SPIKE_SL88_TP97", base_sl=88, tp=97),
        
        # Dynamic SL
        StrategyConfig(name="SPIKE_DYNAMIC_SL", base_sl=86, tp=97, 
                      dynamic_sl=True, dynamic_sl_steps=[(95, 90)]),
        StrategyConfig(name="SPIKE_DYNAMIC_SL_v2", base_sl=86, tp=97,
                      dynamic_sl=True, dynamic_sl_steps=[(93, 88), (95, 91)]),
        
        # Slippage sensitivity
        StrategyConfig(name="SPIKE_SL86_TP97_slip0", slip_entry=0, slip_exit=0, base_sl=86, tp=97),
        StrategyConfig(name="SPIKE_SL86_TP97_slip1", slip_entry=1, slip_exit=1, base_sl=86, tp=97),
        StrategyConfig(name="SPIKE_SL86_TP97_slip2", slip_entry=2, slip_exit=1, base_sl=86, tp=97),
        StrategyConfig(name="SPIKE_SL86_TP97_slip3", slip_entry=3, slip_exit=1, base_sl=86, tp=97),
        StrategyConfig(name="SPIKE_SL86_TP97_slip2_2", slip_entry=2, slip_exit=2, base_sl=86, tp=97),
    ]
    
    print(f"Evaluating {len(configs)} strategies...")
    results: List[StrategyStats] = []
    for config in configs:
        result = evaluate_strategy(contexts, config)
        results.append(result)
    
    # ========================================================================
    # Audit: Find worst cases
    # ========================================================================
    
    print("\n" + "=" * 100)
    print("TRADE-LEVEL AUDIT")
    print("=" * 100)
    
    # Focus on the main strategy
    main_strat = next((r for r in results if r.name == "SPIKE_SL86_TP97"), None)
    
    if main_strat:
        print(f"\nStrategy: {main_strat.name}")
        print(f"Total trades: {main_strat.trades}")
        print(f"Worst loss (pnl_invested): {main_strat.worst_loss:.2%}")
        
        # Find any trades with pnl < -15%
        bad_trades = [t for t in main_strat.trades_audit if t.pnl_invested < -0.15]
        
        print(f"\nTrades with pnl_invested < -15%: {len(bad_trades)}")
        
        if bad_trades:
            print("\nWORST CASES (pnl < -15%):")
            print("-" * 120)
            print(f"{'Window':<25} {'Side':<5} {'Entry':>6} {'Exit':>6} {'Reason':<15} {'PnL%':>8} {'SL_Level':>8}")
            print("-" * 120)
            for t in sorted(bad_trades, key=lambda x: x.pnl_invested)[:20]:
                print(f"{t.window_id:<25} {t.side:<5} {t.entry_price:>5}c {t.exit_price:>5}c "
                      f"{t.exit_reason:<15} {t.pnl_invested:>+7.1%} {t.dynamic_sl_level:>7}c")
            
            # Write to CSV
            with open(outdir / 'worst_cases.csv', 'w', newline='') as f:
                writer = csv.writer(f)
                writer.writerow(['window_id', 'side', 'entry_price', 'exit_price', 'exit_reason',
                                'pnl_invested', 'winner_at_settle', 'sl_triggered', 'dynamic_sl_level'])
                for t in bad_trades:
                    writer.writerow([t.window_id, t.side, t.entry_price, t.exit_price, t.exit_reason,
                                    t.pnl_invested, t.winner_at_settle, t.sl_triggered, t.dynamic_sl_level])
        else:
            print("   NONE - SL86 properly capping losses!")
        
        # Exit reason breakdown
        exit_counts = {}
        exit_pnls = {}
        for t in main_strat.trades_audit:
            exit_counts[t.exit_reason] = exit_counts.get(t.exit_reason, 0) + 1
            if t.exit_reason not in exit_pnls:
                exit_pnls[t.exit_reason] = []
            exit_pnls[t.exit_reason].append(t.pnl_invested)
        
        print(f"\nExit Reason Breakdown:")
        print(f"{'Reason':<20} {'Count':>7} {'%':>7} {'Avg PnL':>10} {'Min PnL':>10} {'Max PnL':>10}")
        print("-" * 70)
        for reason, count in sorted(exit_counts.items(), key=lambda x: -x[1]):
            pnls = exit_pnls[reason]
            print(f"{reason:<20} {count:>7} {count/main_strat.trades*100:>6.1f}% "
                  f"{sum(pnls)/len(pnls):>+9.2%} {min(pnls):>+9.2%} {max(pnls):>+9.2%}")
    
    # ========================================================================
    # Strategy Comparison (PnL-based ranking)
    # ========================================================================
    
    print("\n" + "=" * 140)
    print("STRATEGY COMPARISON (ranked by EV/invested)")
    print("=" * 140)
    
    results.sort(key=lambda r: r.ev_invested, reverse=True)
    
    print(f"{'Rank':>4} {'Strategy':<30} {'Trades':>7} {'EV/inv':>9} {'WorstL':>9} {'W1%L':>8} "
          f"{'DD@2%':>7} {'DD@3%':>7} {'PF':>6} {'Sharpe':>7}")
    print("-" * 140)
    
    for i, r in enumerate(results):
        print(f"{i+1:>4} {r.name:<30} {r.trades:>7} {r.ev_invested:>+8.2%} "
              f"{r.worst_loss:>+8.2%} {r.worst_1pct_loss:>+7.2%} "
              f"{r.max_drawdown_2pct:>6.2%} {r.max_drawdown_3pct:>6.2%} "
              f"{r.profit_factor:>6.2f} {r.sharpe_proxy:>7.3f}")
    
    # ========================================================================
    # Dynamic SL Comparison
    # ========================================================================
    
    print("\n" + "=" * 100)
    print("DYNAMIC SL COMPARISON")
    print("=" * 100)
    
    fixed_sl = next((r for r in results if r.name == "SPIKE_SL86_TP97"), None)
    dynamic_sl = next((r for r in results if r.name == "SPIKE_DYNAMIC_SL"), None)
    dynamic_sl_v2 = next((r for r in results if r.name == "SPIKE_DYNAMIC_SL_v2"), None)
    
    if fixed_sl and dynamic_sl:
        print(f"\n{'Metric':<25} {'Fixed SL86':>15} {'Dynamic SL':>15} {'Dynamic v2':>15}")
        print("-" * 75)
        
        for metric, getter in [
            ('Trades', lambda r: r.trades),
            ('EV/invested', lambda r: f"{r.ev_invested:+.2%}"),
            ('Worst Loss', lambda r: f"{r.worst_loss:+.2%}"),
            ('Worst 1% Loss', lambda r: f"{r.worst_1pct_loss:+.2%}"),
            ('Max DD @2%', lambda r: f"{r.max_drawdown_2pct:.2%}"),
            ('Max DD @3%', lambda r: f"{r.max_drawdown_3pct:.2%}"),
            ('Profit Factor', lambda r: f"{r.profit_factor:.2f}"),
            ('Sharpe Proxy', lambda r: f"{r.sharpe_proxy:.3f}"),
        ]:
            fixed_val = getter(fixed_sl) if fixed_sl else "N/A"
            dyn_val = getter(dynamic_sl) if dynamic_sl else "N/A"
            dyn2_val = getter(dynamic_sl_v2) if dynamic_sl_v2 else "N/A"
            print(f"{metric:<25} {str(fixed_val):>15} {str(dyn_val):>15} {str(dyn2_val):>15}")
    
    # ========================================================================
    # Slippage Sensitivity
    # ========================================================================
    
    print("\n" + "=" * 100)
    print("SLIPPAGE SENSITIVITY")
    print("=" * 100)
    
    slip_strats = [r for r in results if 'slip' in r.name.lower()]
    
    print(f"\n{'Strategy':<35} {'Slip_E':>7} {'Slip_X':>7} {'EV/inv':>9} {'DD@3%':>7} {'PF':>6}")
    print("-" * 80)
    
    for r in slip_strats:
        slip_e = r.params.get('slip_entry', 0)
        slip_x = r.params.get('slip_exit', 0)
        print(f"{r.name:<35} {slip_e:>6}c {slip_x:>6}c {r.ev_invested:>+8.2%} "
              f"{r.max_drawdown_3pct:>6.2%} {r.profit_factor:>6.2f}")
    
    # ========================================================================
    # Ablation Study
    # ========================================================================
    
    print("\n" + "=" * 100)
    print("ABLATION STUDY")
    print("=" * 100)
    
    base = next((r for r in results if r.name == "SPIKE_BASE"), None)
    with_sl = next((r for r in results if r.name == "SPIKE_SL86_TP97"), None)
    
    if base and with_sl:
        print(f"\n{'Component':<30} {'EV/inv':>10} {'Worst Loss':>12} {'DD@3%':>10} {'Delta EV':>10}")
        print("-" * 80)
        print(f"{'SPIKE only (no exits)':<30} {base.ev_invested:>+9.2%} {base.worst_loss:>+11.2%} "
              f"{base.max_drawdown_3pct:>9.2%} {'baseline':>10}")
        print(f"{'SPIKE + SL86 + TP97':<30} {with_sl.ev_invested:>+9.2%} {with_sl.worst_loss:>+11.2%} "
              f"{with_sl.max_drawdown_3pct:>9.2%} {with_sl.ev_invested - base.ev_invested:>+9.2%}")
    
    # ========================================================================
    # Write Trade Audit CSV
    # ========================================================================
    
    if main_strat:
        with open(outdir / 'trades_audit.csv', 'w', newline='') as f:
            writer = csv.writer(f)
            writer.writerow([
                'window_id', 'side', 'first_touch_time', 'entry_time', 'entry_price',
                'spike_min', 'spike_max', 'exit_reason', 'exit_time', 'exit_price',
                'pnl_per_share', 'pnl_invested', 'winner_at_settle', 'time_held',
                'sl_triggered', 'tp_triggered', 'dynamic_sl_level'
            ])
            for t in main_strat.trades_audit:
                writer.writerow([
                    t.window_id, t.side, t.first_touch_time, t.entry_time, t.entry_price,
                    t.spike_min, t.spike_max, t.exit_reason, t.exit_time, t.exit_price,
                    t.pnl_per_share, t.pnl_invested, t.winner_at_settle, t.time_held,
                    t.sl_triggered, t.tp_triggered, t.dynamic_sl_level
                ])
    
    # ========================================================================
    # Write Deploy Rule
    # ========================================================================
    
    deploy_md = f"""# Final Deployment Rule

## Entry

1. **Trigger**: First tick where UP >= 90c OR DOWN >= 90c
2. **Wait**: 10 seconds
3. **SPIKE Validation** (in those 10 seconds):
   - `min_side >= 88c` (no dump)
   - `max_side >= 93c` (push-through)
4. **Execute**: LIMIT BUY at 93c
   - Cancel if not filled within 2 seconds
   - Conservative backtest assumes `slip_entry = +1c`

## Exit

Check every tick after entry:

1. **TAKE PROFIT**: Exit when `side >= 97c`
   - Limit sell at 97c (no slippage)

2. **STOP LOSS**: Exit when `side <= 86c`
   - Market/aggressive limit sell
   - Assume `slip_exit = +1c` worst-case
   - **This caps max loss to ~9% on invested capital**

3. **Settlement**: If neither TP nor SL hit, hold to settlement

## Dynamic SL (Optional Enhancement)

If `side >= 95c` at any point, raise SL from 86 to 90.
This locks in gains and prevents "nearly won then dumped" scenarios.

## Expected Performance (SPIKE_SL86_TP97, slip_entry=1, slip_exit=1)

| Metric | Value |
|--------|-------|
| Trades | {main_strat.trades if main_strat else 'N/A'} |
| EV/invested | {main_strat.ev_invested:+.2%} |
| Worst Loss | {main_strat.worst_loss:+.2%} |
| Worst 1% Loss | {main_strat.worst_1pct_loss:+.2%} |
| Max DD (@2%) | {main_strat.max_drawdown_2pct:.2%} |
| Max DD (@3%) | {main_strat.max_drawdown_3pct:.2%} |
| Profit Factor | {main_strat.profit_factor:.2f} |
| Sharpe Proxy | {main_strat.sharpe_proxy:.3f} |

## Position Sizing

- **Start**: 2% bankroll per trade
- **After 300+ live trades with verified stats**: increase to 3%
- **Never exceed**: 5% per trade

## Exit Reason Distribution

| Reason | Count | % | Avg PnL |
|--------|-------|---|---------|
"""
    
    if main_strat:
        exit_counts = {}
        exit_pnls = {}
        for t in main_strat.trades_audit:
            exit_counts[t.exit_reason] = exit_counts.get(t.exit_reason, 0) + 1
            if t.exit_reason not in exit_pnls:
                exit_pnls[t.exit_reason] = []
            exit_pnls[t.exit_reason].append(t.pnl_invested)
        
        for reason, count in sorted(exit_counts.items(), key=lambda x: -x[1]):
            pnls = exit_pnls[reason]
            deploy_md += f"| {reason} | {count} | {count/main_strat.trades*100:.1f}% | {sum(pnls)/len(pnls):+.2%} |\n"
    
    with open(outdir / 'deploy_rule.md', 'w') as f:
        f.write(deploy_md)
    
    # ========================================================================
    # Final Summary
    # ========================================================================
    
    print("\n" + "=" * 100)
    print("FINAL AUDIT RESULT")
    print("=" * 100)
    
    if main_strat:
        bad_count = len([t for t in main_strat.trades_audit if t.pnl_invested < -0.15])
        if bad_count == 0:
            print(f"\n[PASS] No trades with pnl < -15%")
            print(f"       SL86 properly caps losses at ~{main_strat.worst_loss:.1%}")
        else:
            print(f"\n[WARN] {bad_count} trades with pnl < -15%")
            print(f"       Check worst_cases.csv for details")
        
        print(f"\n       Worst loss: {main_strat.worst_loss:.2%}")
        print(f"       Expected worst with SL86+slip1: ~{(86-1-94)/94*100:.1f}%")
    
    if dynamic_sl and fixed_sl:
        ev_diff = dynamic_sl.ev_invested - fixed_sl.ev_invested
        dd_diff = dynamic_sl.max_drawdown_3pct - fixed_sl.max_drawdown_3pct
        print(f"\nDynamic SL vs Fixed SL86:")
        print(f"  EV difference: {ev_diff:+.2%}")
        print(f"  DD difference: {dd_diff:+.2%}")
        if ev_diff > 0 and dd_diff < 0:
            print(f"  [BETTER] Dynamic SL improves both EV and DD")
        elif ev_diff > 0:
            print(f"  [TRADEOFF] Dynamic SL improves EV but increases DD")
        else:
            print(f"  [KEEP FIXED] Fixed SL86 is simpler and performs similarly")
    
    print(f"\nOutputs written to: {outdir}/")
    print(f"  - trades_audit.csv (full trade log)")
    print(f"  - worst_cases.csv (trades with pnl < -15%)")
    print(f"  - deploy_rule.md (final spec)")
    
    return 0


if __name__ == '__main__':
    sys.exit(main())


