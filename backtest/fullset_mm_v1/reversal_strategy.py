"""
REVERSAL TRADING STRATEGY

When we detect a high-probability reversal:
1. BUY the opposite side (it's cheap and will recover)
2. HOLD to settlement (or exit on recovery)

This exploits the 42% reversal rate we found in the data.
"""
from dataclasses import dataclass, field
from typing import List, Dict, Optional, Tuple
from collections import defaultdict
from enum import Enum
import json
import os
import csv

from .parse import find_window_ids, load_window_ticks
from .stream import merge_tick_streams, QuoteTick
from .config import DEFAULT_BUY_DIR, DEFAULT_SELL_DIR


class TradeType(Enum):
    FADE_SPIKE = "fade_spike"  # Buy opposite when spike looks weak
    SAFE_DIRECTIONAL = "safe_directional"  # Buy spike when safe
    FULL_SET = "full_set"  # Buy both sides


@dataclass
class ReversalTrade:
    """A trade based on reversal detection."""
    window_id: str
    trade_type: TradeType
    
    # Entry
    entry_time: float
    entry_side: str  # Which side we bought
    entry_price: int
    spike_side: str  # Which side spiked (may be same or opposite)
    spike_price: int
    reversal_score: int
    
    # Exit
    exit_time: float
    exit_price: int
    exit_reason: str  # "settlement", "target", "stop"
    
    # PnL
    gross_pnl_cents: float
    fee: float
    net_pnl_cents: float
    
    # Features at entry
    spike_speed: float
    opposite_trend: float
    time_remaining: float


def compute_reversal_score(
    spike_speed_5s: float,
    opposite_trend: float,
    spread: int,
    time_remaining: float,
    combined_cost: int,
    consecutive_up: int
) -> int:
    """Compute the reversal risk score."""
    score = 0
    
    # Positive = more likely to reverse
    if spike_speed_5s > 5:
        score += 2
    elif spike_speed_5s > 3:
        score += 1
    
    if opposite_trend > 0:
        score += 2
    
    if spread > 3:
        score += 1
    
    if time_remaining > 300:
        score += 1
    
    if combined_cost >= 100:
        score += 1
    
    if consecutive_up < 3:
        score += 1
    
    # Negative = safer entry
    if opposite_trend < -5:
        score -= 2
    elif opposite_trend < -2:
        score -= 1
    
    if combined_cost < 98:
        score -= 1
    
    if spread < 2:
        score -= 1
    
    if consecutive_up >= 5:
        score -= 1
    
    return score


def polymarket_fee(price_cents: int, size_dollars: float) -> float:
    """Calculate Polymarket taker fee."""
    if price_cents <= 0 or price_cents >= 100:
        return 0
    p = price_cents / 100.0
    shares = size_dollars / (price_cents / 100.0)
    fee_per_share = 0.25 * (p * (1 - p)) ** 2
    return shares * fee_per_share


@dataclass
class StrategyConfig:
    """Configuration for reversal strategy."""
    # Entry thresholds
    spike_threshold: int = 90  # Price to consider a "spike"
    min_reversal_score_fade: int = 4  # Score to trigger fade trade
    max_reversal_score_safe: int = 1  # Score for safe directional
    
    # Full-set parameters
    max_combined_for_fullset: int = 99
    
    # Sizing
    size_per_trade: float = 10.0
    
    # Exit parameters
    target_profit_pct: float = 50.0  # Exit if opposite recovers 50%+
    stop_loss_pct: float = 50.0  # Exit if it drops 50% more
    
    # What to trade
    trade_fades: bool = True
    trade_safe_directional: bool = True
    trade_fullset: bool = True


class ReversalStrategyBacktest:
    """Backtest the reversal trading strategy."""
    
    def __init__(self, config: StrategyConfig):
        self.config = config
    
    def run_window(self, window_id: str, buy_dir: str, sell_dir: str) -> List[ReversalTrade]:
        """Run strategy on a single window."""
        buy_ticks, sell_ticks = load_window_ticks(window_id, buy_dir, sell_dir)
        if len(buy_ticks) < 20 or len(sell_ticks) < 20:
            return []
        
        merged = merge_tick_streams(buy_ticks, sell_ticks)
        if len(merged) < 20:
            return []
        
        trades = []
        final = merged[-1]
        
        # Track history for feature calculation
        up_history = []
        down_history = []
        
        # Track if we've already traded this window
        traded_fade = False
        traded_safe = False
        traded_fullset = False
        
        for i, tick in enumerate(merged):
            t = tick.elapsed_secs
            time_remaining = 900 - t
            
            # Update history
            up_history.append((t, tick.up_ask, tick.up_bid))
            down_history.append((t, tick.down_ask, tick.down_bid))
            
            # Skip if too early or too late
            if t < 10 or time_remaining < 30:
                continue
            
            # Check for full-set opportunity first
            combined = tick.up_ask + tick.down_ask
            if self.config.trade_fullset and not traded_fullset and combined <= self.config.max_combined_for_fullset:
                trade = self._execute_fullset(
                    window_id, tick, t, time_remaining, 
                    final, combined
                )
                if trade:
                    trades.append(trade)
                    traded_fullset = True
                    continue
            
            # Check for spike and compute features
            for spike_side in ["UP", "DOWN"]:
                if spike_side == "UP":
                    spike_price = tick.up_ask
                    opp_price = tick.down_ask
                    history = up_history
                    opp_history = down_history
                    final_spike = final.up_ask
                    final_opp = final.down_ask
                else:
                    spike_price = tick.down_ask
                    opp_price = tick.up_ask
                    history = down_history
                    opp_history = up_history
                    final_spike = final.down_ask
                    final_opp = final.up_ask
                
                if spike_price < self.config.spike_threshold:
                    continue
                
                # Calculate features
                speed_5s = self._calc_speed(history, t, 5)
                opp_trend = self._calc_trend(opp_history, t, 5)
                spread = spike_price - (tick.up_bid if spike_side == "UP" else tick.down_bid)
                consecutive_up = self._count_consecutive_up(history)
                
                score = compute_reversal_score(
                    speed_5s, opp_trend, spread, 
                    time_remaining, combined, consecutive_up
                )
                
                # Decision: Fade or Safe entry?
                if self.config.trade_fades and not traded_fade and score >= self.config.min_reversal_score_fade:
                    # FADE: Buy the opposite side
                    trade = self._execute_fade(
                        window_id, tick, t, time_remaining,
                        spike_side, spike_price, opp_price,
                        final_opp, score, speed_5s, opp_trend
                    )
                    if trade:
                        trades.append(trade)
                        traded_fade = True
                
                elif self.config.trade_safe_directional and not traded_safe and score <= self.config.max_reversal_score_safe:
                    # SAFE: Buy the spike side
                    trade = self._execute_safe_directional(
                        window_id, tick, t, time_remaining,
                        spike_side, spike_price,
                        final_spike, score, speed_5s, opp_trend
                    )
                    if trade:
                        trades.append(trade)
                        traded_safe = True
        
        return trades
    
    def _calc_speed(self, history: List, current_time: float, seconds: float) -> float:
        """Calculate price change speed over last N seconds."""
        if len(history) < 2:
            return 0
        current_price = history[-1][1]
        for ht, hp, _ in reversed(history[:-1]):
            if current_time - ht >= seconds:
                return (current_price - hp) / seconds
        return 0
    
    def _calc_trend(self, history: List, current_time: float, seconds: float) -> float:
        """Calculate price trend over last N seconds."""
        if len(history) < 2:
            return 0
        current_price = history[-1][1]
        for ht, hp, _ in reversed(history[:-1]):
            if current_time - ht >= seconds:
                return current_price - hp
        return 0
    
    def _count_consecutive_up(self, history: List) -> int:
        """Count consecutive up ticks."""
        count = 0
        for j in range(len(history) - 2, -1, -1):
            if history[j+1][1] > history[j][1]:
                count += 1
            else:
                break
        return count
    
    def _execute_fade(
        self, window_id: str, tick: QuoteTick, t: float, time_remaining: float,
        spike_side: str, spike_price: int, opp_price: int,
        final_opp: int, score: int, speed: float, opp_trend: float
    ) -> Optional[ReversalTrade]:
        """Execute a fade trade (buy opposite side of spike)."""
        # Entry: buy the opposite side
        entry_side = "DOWN" if spike_side == "UP" else "UP"
        entry_price = opp_price
        
        # Calculate PnL
        # If opposite wins (spike reverses), we get 100c
        # If spike wins, we get 0c
        exit_price = final_opp
        
        if exit_price >= 97:
            # We won!
            gross_pnl = (100 - entry_price) / 100 * self.config.size_per_trade
        else:
            # We lost
            gross_pnl = -entry_price / 100 * self.config.size_per_trade
        
        fee = polymarket_fee(entry_price, self.config.size_per_trade)
        net_pnl = gross_pnl - fee
        
        return ReversalTrade(
            window_id=window_id,
            trade_type=TradeType.FADE_SPIKE,
            entry_time=t,
            entry_side=entry_side,
            entry_price=entry_price,
            spike_side=spike_side,
            spike_price=spike_price,
            reversal_score=score,
            exit_time=900,
            exit_price=exit_price,
            exit_reason="settlement",
            gross_pnl_cents=gross_pnl * 100,
            fee=fee,
            net_pnl_cents=net_pnl * 100,
            spike_speed=speed,
            opposite_trend=opp_trend,
            time_remaining=time_remaining
        )
    
    def _execute_safe_directional(
        self, window_id: str, tick: QuoteTick, t: float, time_remaining: float,
        spike_side: str, spike_price: int,
        final_spike: int, score: int, speed: float, opp_trend: float
    ) -> Optional[ReversalTrade]:
        """Execute a safe directional trade (buy the safe spike)."""
        entry_price = spike_price
        exit_price = final_spike
        
        if exit_price >= 97:
            gross_pnl = (100 - entry_price) / 100 * self.config.size_per_trade
        else:
            gross_pnl = -entry_price / 100 * self.config.size_per_trade
        
        fee = polymarket_fee(entry_price, self.config.size_per_trade)
        net_pnl = gross_pnl - fee
        
        return ReversalTrade(
            window_id=window_id,
            trade_type=TradeType.SAFE_DIRECTIONAL,
            entry_time=t,
            entry_side=spike_side,
            entry_price=entry_price,
            spike_side=spike_side,
            spike_price=spike_price,
            reversal_score=score,
            exit_time=900,
            exit_price=exit_price,
            exit_reason="settlement",
            gross_pnl_cents=gross_pnl * 100,
            fee=fee,
            net_pnl_cents=net_pnl * 100,
            spike_speed=speed,
            opposite_trend=opp_trend,
            time_remaining=time_remaining
        )
    
    def _execute_fullset(
        self, window_id: str, tick: QuoteTick, t: float, time_remaining: float,
        final: QuoteTick, combined: int
    ) -> Optional[ReversalTrade]:
        """Execute a full-set trade."""
        edge = 100 - combined
        
        up_fee = polymarket_fee(tick.up_ask, self.config.size_per_trade)
        down_fee = polymarket_fee(tick.down_ask, self.config.size_per_trade)
        total_fee = up_fee + down_fee
        
        gross_pnl = edge / 100 * self.config.size_per_trade * 2
        net_pnl = gross_pnl - total_fee
        
        return ReversalTrade(
            window_id=window_id,
            trade_type=TradeType.FULL_SET,
            entry_time=t,
            entry_side="BOTH",
            entry_price=combined,
            spike_side="N/A",
            spike_price=0,
            reversal_score=0,
            exit_time=900,
            exit_price=100,
            exit_reason="settlement",
            gross_pnl_cents=gross_pnl * 100,
            fee=total_fee,
            net_pnl_cents=net_pnl * 100,
            spike_speed=0,
            opposite_trend=0,
            time_remaining=time_remaining
        )


def run_backtest(
    config: StrategyConfig,
    buy_dir: str = DEFAULT_BUY_DIR,
    sell_dir: str = DEFAULT_SELL_DIR
) -> Dict:
    """Run full backtest."""
    print("=" * 70)
    print("REVERSAL STRATEGY BACKTEST")
    print("=" * 70)
    
    print(f"\nConfig:")
    print(f"  Spike threshold: {config.spike_threshold}c")
    print(f"  Min score for fade: {config.min_reversal_score_fade}")
    print(f"  Max score for safe: {config.max_reversal_score_safe}")
    print(f"  Size per trade: ${config.size_per_trade}")
    print(f"  Trade fades: {config.trade_fades}")
    print(f"  Trade safe directional: {config.trade_safe_directional}")
    print(f"  Trade full-set: {config.trade_fullset}")
    
    buy_ids = set(find_window_ids(buy_dir))
    sell_ids = set(find_window_ids(sell_dir))
    common = sorted(buy_ids & sell_ids)
    
    print(f"\nProcessing {len(common)} windows...")
    
    strategy = ReversalStrategyBacktest(config)
    all_trades = []
    
    for i, wid in enumerate(common):
        if i % 500 == 0:
            print(f"  {i}/{len(common)}...")
        trades = strategy.run_window(wid, buy_dir, sell_dir)
        all_trades.extend(trades)
    
    # Analyze results
    results = analyze_results(all_trades, len(common))
    
    return results


def analyze_results(trades: List[ReversalTrade], total_windows: int) -> Dict:
    """Analyze backtest results."""
    print("\n" + "=" * 70)
    print("BACKTEST RESULTS")
    print("=" * 70)
    
    if not trades:
        print("No trades executed!")
        return {}
    
    # Group by trade type
    by_type = defaultdict(list)
    for t in trades:
        by_type[t.trade_type].append(t)
    
    results = {
        "total_windows": total_windows,
        "total_trades": len(trades),
        "by_type": {}
    }
    
    print(f"\nTotal windows: {total_windows}")
    print(f"Total trades: {len(trades)}")
    
    for trade_type, type_trades in by_type.items():
        print(f"\n--- {trade_type.value.upper()} ---")
        
        n = len(type_trades)
        wins = sum(1 for t in type_trades if t.net_pnl_cents > 0)
        losses = n - wins
        win_rate = wins / n * 100
        
        gross_pnl = sum(t.gross_pnl_cents for t in type_trades)
        total_fees = sum(t.fee for t in type_trades)
        net_pnl = sum(t.net_pnl_cents for t in type_trades)
        
        avg_entry = sum(t.entry_price for t in type_trades) / n
        avg_score = sum(t.reversal_score for t in type_trades) / n
        
        print(f"  Trades: {n}")
        print(f"  Win/Loss: {wins}/{losses} ({win_rate:.1f}%)")
        print(f"  Avg entry price: {avg_entry:.1f}c")
        print(f"  Avg reversal score: {avg_score:.1f}")
        print(f"  Gross PnL: {gross_pnl:.0f}c (${gross_pnl/100:.2f})")
        print(f"  Fees: ${total_fees:.2f}")
        print(f"  NET PnL: {net_pnl:.0f}c (${net_pnl/100:.2f})")
        
        if n > 0:
            print(f"  Avg PnL/trade: {net_pnl/n:.2f}c")
        
        results["by_type"][trade_type.value] = {
            "trades": n,
            "wins": wins,
            "win_rate": win_rate,
            "gross_pnl": gross_pnl,
            "fees": total_fees,
            "net_pnl": net_pnl
        }
    
    # Overall summary
    total_net = sum(t.net_pnl_cents for t in trades)
    total_fees = sum(t.fee for t in trades)
    
    print(f"\n{'='*50}")
    print("OVERALL")
    print(f"{'='*50}")
    print(f"Total trades: {len(trades)}")
    print(f"Total fees: ${total_fees:.2f}")
    print(f"NET PnL: {total_net:.0f}c (${total_net/100:.2f})")
    print(f"PnL per window: {total_net/total_windows:.2f}c")
    print(f"Annualized (at 24 windows/day): ${total_net/100/51*365:.2f}")
    
    results["total_net_pnl"] = total_net
    results["total_fees"] = total_fees
    
    return results


def run_sensitivity_analysis():
    """Test different reversal score thresholds."""
    print("=" * 70)
    print("SENSITIVITY ANALYSIS: Finding Optimal Reversal Score Thresholds")
    print("=" * 70)
    
    results = []
    
    # Test different fade thresholds
    for fade_thresh in [3, 4, 5, 6]:
        for safe_thresh in [-1, 0, 1, 2]:
            config = StrategyConfig(
                spike_threshold=90,
                min_reversal_score_fade=fade_thresh,
                max_reversal_score_safe=safe_thresh,
                trade_fades=True,
                trade_safe_directional=True,
                trade_fullset=True,
                size_per_trade=10.0
            )
            
            print(f"\n--- Fade >= {fade_thresh}, Safe <= {safe_thresh} ---")
            result = run_backtest(config)
            
            if result:
                results.append({
                    "fade_thresh": fade_thresh,
                    "safe_thresh": safe_thresh,
                    **result
                })
    
    # Find best combo
    print("\n" + "=" * 70)
    print("SENSITIVITY SUMMARY")
    print("=" * 70)
    print(f"{'Fade':<8} {'Safe':<8} {'Trades':<10} {'Net PnL':<15} {'$/window'}")
    print("-" * 60)
    
    for r in sorted(results, key=lambda x: x.get("total_net_pnl", 0), reverse=True):
        net = r.get("total_net_pnl", 0)
        trades = r.get("total_trades", 0)
        windows = r.get("total_windows", 1)
        per_window = net / windows
        print(f"{r['fade_thresh']:<8} {r['safe_thresh']:<8} {trades:<10} ${net/100:<14.2f} {per_window:.2f}c")


def save_results(trades: List[ReversalTrade], outdir: str = "out_reversal_backtest"):
    """Save backtest results."""
    os.makedirs(outdir, exist_ok=True)
    
    # Save trades CSV
    with open(os.path.join(outdir, "trades.csv"), "w", newline="") as f:
        writer = csv.writer(f)
        writer.writerow([
            "window_id", "trade_type", "entry_time", "entry_side", "entry_price",
            "spike_side", "spike_price", "reversal_score", "exit_price",
            "gross_pnl", "fee", "net_pnl", "spike_speed", "opp_trend", "time_remaining"
        ])
        for t in trades:
            writer.writerow([
                t.window_id, t.trade_type.value, t.entry_time, t.entry_side, t.entry_price,
                t.spike_side, t.spike_price, t.reversal_score, t.exit_price,
                f"{t.gross_pnl_cents:.2f}", f"{t.fee:.4f}", f"{t.net_pnl_cents:.2f}",
                f"{t.spike_speed:.2f}", f"{t.opposite_trend:.1f}", f"{t.time_remaining:.0f}"
            ])
    
    print(f"\nResults saved to {outdir}/")


def main():
    """Run the reversal strategy backtest."""
    import sys
    
    if len(sys.argv) > 1 and sys.argv[1] == "--sensitivity":
        run_sensitivity_analysis()
    else:
        # Default config
        config = StrategyConfig(
            spike_threshold=90,
            min_reversal_score_fade=4,
            max_reversal_score_safe=1,
            trade_fades=True,
            trade_safe_directional=True,
            trade_fullset=True,
            size_per_trade=10.0
        )
        
        result = run_backtest(config)
        
        # Also save detailed results
        buy_ids = set(find_window_ids(DEFAULT_BUY_DIR))
        sell_ids = set(find_window_ids(DEFAULT_SELL_DIR))
        common = sorted(buy_ids & sell_ids)
        
        strategy = ReversalStrategyBacktest(config)
        all_trades = []
        for wid in common:
            trades = strategy.run_window(wid, DEFAULT_BUY_DIR, DEFAULT_SELL_DIR)
            all_trades.extend(trades)
        
        save_results(all_trades)


if __name__ == "__main__":
    main()

