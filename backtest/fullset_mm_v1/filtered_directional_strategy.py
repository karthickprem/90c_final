"""
FILTERED DIRECTIONAL STRATEGY

Use reversal score to FILTER entries:
- Low score (< 2) = SAFE to enter the spike side
- High score (> 3) = DON'T enter, too risky

Hold to settlement (no intraday exit - spread + fees kill profit)
"""
from dataclasses import dataclass
from typing import List, Dict
from collections import defaultdict
import os
import csv

from .parse import find_window_ids, load_window_ticks
from .stream import merge_tick_streams, QuoteTick
from .config import DEFAULT_BUY_DIR, DEFAULT_SELL_DIR


def polymarket_fee(price_cents: int, size_dollars: float) -> float:
    """Calculate Polymarket taker fee."""
    if price_cents <= 0 or price_cents >= 100:
        return 0
    p = price_cents / 100.0
    shares = size_dollars / (price_cents / 100.0)
    fee_per_share = 0.25 * (p * (1 - p)) ** 2
    return shares * fee_per_share


def compute_features(merged: List[QuoteTick], idx: int, spike_side: str):
    """Compute reversal risk features for a spike."""
    tick = merged[idx]
    t = tick.elapsed_secs
    
    if spike_side == "UP":
        spike_price = tick.up_ask
        opp_price = tick.down_ask
        spread = tick.up_ask - tick.up_bid
    else:
        spike_price = tick.down_ask
        opp_price = tick.up_ask
        spread = tick.down_ask - tick.down_bid
    
    # Spike speed over last 5 seconds
    speed_5s = 0
    for j in range(idx - 1, -1, -1):
        if t - merged[j].elapsed_secs >= 5:
            if spike_side == "UP":
                speed_5s = (spike_price - merged[j].up_ask) / 5
            else:
                speed_5s = (spike_price - merged[j].down_ask) / 5
            break
    
    # Opposite side trend
    opp_trend = 0
    for j in range(idx - 1, -1, -1):
        if t - merged[j].elapsed_secs >= 5:
            if spike_side == "UP":
                opp_trend = opp_price - merged[j].down_ask
            else:
                opp_trend = opp_price - merged[j].up_ask
            break
    
    # Momentum (consecutive up ticks)
    consecutive = 0
    for j in range(idx - 1, 0, -1):
        if spike_side == "UP":
            if merged[j].up_ask < merged[j+1].up_ask:
                consecutive += 1
            else:
                break
        else:
            if merged[j].down_ask < merged[j+1].down_ask:
                consecutive += 1
            else:
                break
    
    time_remaining = 900 - t
    combined_cost = tick.up_ask + tick.down_ask
    
    return {
        "speed_5s": speed_5s,
        "opp_trend": opp_trend,
        "spread": spread,
        "consecutive": consecutive,
        "time_remaining": time_remaining,
        "combined_cost": combined_cost
    }


def compute_reversal_score(features: Dict) -> int:
    """Compute reversal risk score (-2 to 6+)."""
    score = 0
    
    # High risk indicators
    if features["speed_5s"] > 5:
        score += 2
    elif features["speed_5s"] > 3:
        score += 1
    
    if features["opp_trend"] > 0:
        score += 2
    
    if features["spread"] > 3:
        score += 1
    
    if features["time_remaining"] > 300:
        score += 1
    
    if features["combined_cost"] >= 100:
        score += 1
    
    if features["consecutive"] < 3:
        score += 1
    
    # Low risk indicators
    if features["opp_trend"] < -5:
        score -= 2
    elif features["opp_trend"] < -2:
        score -= 1
    
    if features["combined_cost"] < 98:
        score -= 1
    
    if features["spread"] < 2:
        score -= 1
    
    if features["consecutive"] >= 5:
        score -= 1
    
    return score


@dataclass
class DirectionalTrade:
    """A directional trade."""
    window_id: str
    side: str
    entry_price: int
    entry_time: float
    reversal_score: int
    
    # Features at entry
    speed_5s: float
    opp_trend: float
    spread: int
    time_remaining: float
    
    # Outcome
    won: bool
    final_price: int
    
    # PnL
    gross_pnl: float
    fee: float
    net_pnl: float


def run_backtest(
    entry_threshold: int = 90,
    max_score: int = 1,  # Only enter if score <= this
    size_per_trade: float = 10.0
) -> List[DirectionalTrade]:
    """Run filtered directional strategy."""
    buy_ids = set(find_window_ids(DEFAULT_BUY_DIR))
    sell_ids = set(find_window_ids(DEFAULT_SELL_DIR))
    common = sorted(buy_ids & sell_ids)
    
    print("=" * 70)
    print("FILTERED DIRECTIONAL STRATEGY")
    print("=" * 70)
    print(f"\nConfig:")
    print(f"  Entry threshold: {entry_threshold}c")
    print(f"  Max reversal score: {max_score}")
    print(f"  Size: ${size_per_trade}")
    print(f"\nProcessing {len(common)} windows...")
    
    all_trades = []
    
    for i, wid in enumerate(common):
        if i % 1000 == 0:
            print(f"  {i}/{len(common)}...")
        
        buy_ticks, sell_ticks = load_window_ticks(wid, DEFAULT_BUY_DIR, DEFAULT_SELL_DIR)
        if len(buy_ticks) < 20 or len(sell_ticks) < 20:
            continue
        
        merged = merge_tick_streams(buy_ticks, sell_ticks)
        if len(merged) < 20:
            continue
        
        final = merged[-1]
        
        up_traded = False
        down_traded = False
        
        for idx, tick in enumerate(merged):
            t = tick.elapsed_secs
            
            # Only trade in first 10 minutes, not last 30 seconds
            if t < 10 or t > 570:
                continue
            
            # Check UP spike
            if not up_traded and tick.up_ask >= entry_threshold:
                features = compute_features(merged, idx, "UP")
                score = compute_reversal_score(features)
                
                if score <= max_score:
                    # Safe to enter!
                    won = final.up_ask >= 97
                    
                    if won:
                        gross = (100 - tick.up_ask) / 100 * size_per_trade
                    else:
                        gross = -tick.up_ask / 100 * size_per_trade
                    
                    fee = polymarket_fee(tick.up_ask, size_per_trade)
                    
                    trade = DirectionalTrade(
                        window_id=wid,
                        side="UP",
                        entry_price=tick.up_ask,
                        entry_time=t,
                        reversal_score=score,
                        speed_5s=features["speed_5s"],
                        opp_trend=features["opp_trend"],
                        spread=features["spread"],
                        time_remaining=features["time_remaining"],
                        won=won,
                        final_price=final.up_ask,
                        gross_pnl=gross,
                        fee=fee,
                        net_pnl=gross - fee
                    )
                    all_trades.append(trade)
                    up_traded = True
            
            # Check DOWN spike
            if not down_traded and tick.down_ask >= entry_threshold:
                features = compute_features(merged, idx, "DOWN")
                score = compute_reversal_score(features)
                
                if score <= max_score:
                    won = final.down_ask >= 97
                    
                    if won:
                        gross = (100 - tick.down_ask) / 100 * size_per_trade
                    else:
                        gross = -tick.down_ask / 100 * size_per_trade
                    
                    fee = polymarket_fee(tick.down_ask, size_per_trade)
                    
                    trade = DirectionalTrade(
                        window_id=wid,
                        side="DOWN",
                        entry_price=tick.down_ask,
                        entry_time=t,
                        reversal_score=score,
                        speed_5s=features["speed_5s"],
                        opp_trend=features["opp_trend"],
                        spread=features["spread"],
                        time_remaining=features["time_remaining"],
                        won=won,
                        final_price=final.down_ask,
                        gross_pnl=gross,
                        fee=fee,
                        net_pnl=gross - fee
                    )
                    all_trades.append(trade)
                    down_traded = True
    
    return all_trades


def analyze_trades(trades: List[DirectionalTrade], days: int = 51):
    """Analyze results."""
    print("\n" + "=" * 70)
    print("RESULTS")
    print("=" * 70)
    
    if not trades:
        print("No trades!")
        return
    
    n = len(trades)
    wins = sum(1 for t in trades if t.won)
    
    gross = sum(t.gross_pnl for t in trades)
    fees = sum(t.fee for t in trades)
    net = sum(t.net_pnl for t in trades)
    
    avg_entry = sum(t.entry_price for t in trades) / n
    avg_score = sum(t.reversal_score for t in trades) / n
    
    print(f"\nTotal trades: {n}")
    print(f"Win/Loss: {wins}/{n-wins} ({wins/n*100:.1f}%)")
    print(f"Avg entry: {avg_entry:.1f}c")
    print(f"Avg reversal score: {avg_score:.1f}")
    
    print(f"\nGross PnL: ${gross:.2f}")
    print(f"Fees: ${fees:.2f}")
    print(f"NET PnL: ${net:.2f}")
    
    # Break-even check
    print(f"\n--- Break-even analysis ---")
    print(f"At {avg_entry:.0f}c entry, need {avg_entry/(100-avg_entry+avg_entry)*100:.1f}% win rate to break even")
    print(f"Actual win rate: {wins/n*100:.1f}%")
    
    # By reversal score
    print("\n--- By Reversal Score ---")
    by_score = defaultdict(lambda: {"n": 0, "wins": 0, "gross": 0, "net": 0})
    for t in trades:
        by_score[t.reversal_score]["n"] += 1
        if t.won:
            by_score[t.reversal_score]["wins"] += 1
        by_score[t.reversal_score]["gross"] += t.gross_pnl
        by_score[t.reversal_score]["net"] += t.net_pnl
    
    print(f"Score    N       Wins    WinRate   Net PnL")
    print("-" * 50)
    for score in sorted(by_score.keys()):
        s = by_score[score]
        wr = s["wins"] / s["n"] * 100 if s["n"] > 0 else 0
        print(f"{score:>5}    {s['n']:<7} {s['wins']:<7} {wr:>5.1f}%    ${s['net']:>7.2f}")
    
    # Projection
    print(f"\n--- Projection ---")
    print(f"PnL over {days} days: ${net:.2f}")
    print(f"PnL per day: ${net/days:.2f}")
    print(f"PnL per 30 days: ${net/days*30:.2f}")
    print(f"Annual: ${net/days*365:.2f}")


def run_score_sensitivity():
    """Test different max score thresholds."""
    print("=" * 70)
    print("SENSITIVITY: Max Reversal Score")
    print("=" * 70)
    
    results = []
    
    for max_score in [-2, -1, 0, 1, 2, 3, 4, 5]:
        trades = run_backtest(90, max_score, 10.0)
        
        if trades:
            n = len(trades)
            wins = sum(1 for t in trades if t.won)
            net = sum(t.net_pnl for t in trades)
            
            results.append({
                "max_score": max_score,
                "trades": n,
                "win_rate": wins/n*100 if n > 0 else 0,
                "net_pnl": net
            })
        else:
            results.append({
                "max_score": max_score,
                "trades": 0,
                "win_rate": 0,
                "net_pnl": 0
            })
    
    print("\n" + "=" * 70)
    print("SUMMARY")
    print("=" * 70)
    print(f"{'MaxScore':>10} {'Trades':>8} {'WinRate':>10} {'Net PnL':>12} {'$/trade':>10}")
    print("-" * 55)
    
    for r in results:
        per_trade = r["net_pnl"] / r["trades"] if r["trades"] > 0 else 0
        print(f"{r['max_score']:>10} {r['trades']:>8} {r['win_rate']:>9.1f}% ${r['net_pnl']:>10.2f} ${per_trade:>9.4f}")


def main():
    import sys
    
    if len(sys.argv) > 1 and sys.argv[1] == "--sensitivity":
        run_score_sensitivity()
    else:
        # Default: only enter on very low reversal score
        trades = run_backtest(
            entry_threshold=90,
            max_score=1,
            size_per_trade=10.0
        )
        
        analyze_trades(trades, 51)


if __name__ == "__main__":
    main()

