"""
STRATEGY COMPARISON - Find the best profitable approach
"""
from collections import defaultdict
import sys

from .parse import find_window_ids, load_window_ticks
from .stream import merge_tick_streams
from .config import DEFAULT_BUY_DIR, DEFAULT_SELL_DIR


def polymarket_fee(price_cents: int, size_dollars: float) -> float:
    if price_cents <= 0 or price_cents >= 100:
        return 0
    p = price_cents / 100.0
    shares = size_dollars / (price_cents / 100.0)
    fee_per_share = 0.25 * (p * (1 - p)) ** 2
    return shares * fee_per_share


def compute_reversal_score(merged, idx, spike_side):
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
    
    speed_5s = 0
    for j in range(idx - 1, -1, -1):
        if t - merged[j].elapsed_secs >= 5:
            if spike_side == "UP":
                speed_5s = (spike_price - merged[j].up_ask) / 5
            else:
                speed_5s = (spike_price - merged[j].down_ask) / 5
            break
    
    opp_trend = 0
    for j in range(idx - 1, -1, -1):
        if t - merged[j].elapsed_secs >= 5:
            if spike_side == "UP":
                opp_trend = opp_price - merged[j].down_ask
            else:
                opp_trend = opp_price - merged[j].up_ask
            break
    
    combined = tick.up_ask + tick.down_ask
    time_remaining = 900 - t
    
    score = 0
    if speed_5s > 5:
        score += 2
    elif speed_5s > 3:
        score += 1
    if opp_trend > 0:
        score += 2
    if spread > 3:
        score += 1
    if time_remaining > 300:
        score += 1
    if combined >= 100:
        score += 1
    
    if opp_trend < -5:
        score -= 2
    elif opp_trend < -2:
        score -= 1
    if combined < 98:
        score -= 1
    if spread < 2:
        score -= 1
    
    return score


def main():
    sys.stdout.reconfigure(encoding='utf-8')
    
    buy_ids = set(find_window_ids(DEFAULT_BUY_DIR))
    sell_ids = set(find_window_ids(DEFAULT_SELL_DIR))
    common = sorted(buy_ids & sell_ids)
    
    print("=" * 70)
    print("STRATEGY COMPARISON")
    print("=" * 70)
    print(f"\nLoading {len(common)} windows...")
    
    # Collect all opportunities
    fullset_opps = []  # (window, combined_cost, net_profit)
    directional_opps = []  # (window, entry_price, score, won, net_profit)
    
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
        
        # Check for full-set
        best_fullset = None
        for tick in merged:
            combined = tick.up_ask + tick.down_ask
            if combined <= 96:  # Potential opportunity
                edge = 100 - combined
                gross = edge / 100 * 10.0 * 2
                up_fee = polymarket_fee(tick.up_ask, 10.0)
                down_fee = polymarket_fee(tick.down_ask, 10.0)
                net = gross - up_fee - down_fee
                
                if best_fullset is None or net > best_fullset[1]:
                    best_fullset = (combined, net)
        
        if best_fullset:
            fullset_opps.append((wid, best_fullset[0], best_fullset[1]))
        
        # Check for directional
        for idx, tick in enumerate(merged):
            t = tick.elapsed_secs
            if t < 10 or t > 570:
                continue
            
            for side in ["UP", "DOWN"]:
                price = tick.up_ask if side == "UP" else tick.down_ask
                final_price = final.up_ask if side == "UP" else final.down_ask
                
                if price >= 92:
                    score = compute_reversal_score(merged, idx, side)
                    won = final_price >= 97
                    
                    if won:
                        gross = (100 - price) / 100 * 10.0
                    else:
                        gross = -price / 100 * 10.0
                    
                    fee = polymarket_fee(price, 10.0)
                    net = gross - fee
                    
                    directional_opps.append((wid, price, score, won, net))
                    break  # One per side per window
    
    # Analyze full-set
    print("\n" + "=" * 70)
    print("FULL-SET ONLY STRATEGY")
    print("=" * 70)
    
    for max_cost in [90, 92, 94, 95, 96]:
        filtered = [o for o in fullset_opps if o[1] <= max_cost]
        if not filtered:
            continue
        total_net = sum(o[2] for o in filtered)
        print(f"  Combined <= {max_cost}c: {len(filtered)} trades, net ${total_net:.2f}, avg ${total_net/len(filtered):.3f}")
    
    # Analyze directional by entry price and score
    print("\n" + "=" * 70)
    print("DIRECTIONAL STRATEGY (by entry price and max score)")
    print("=" * 70)
    
    results = []
    for entry_min in [92, 93, 94, 95]:
        for max_score in [-2, -1, 0, 1]:
            filtered = [o for o in directional_opps if o[1] >= entry_min and o[2] <= max_score]
            if len(filtered) < 5:
                continue
            
            wins = sum(1 for o in filtered if o[3])
            total_net = sum(o[4] for o in filtered)
            win_rate = wins / len(filtered) * 100
            
            results.append({
                "entry": entry_min,
                "score": max_score,
                "n": len(filtered),
                "wins": wins,
                "wr": win_rate,
                "net": total_net
            })
    
    print(f"{'Entry':>6} {'Score':>6} {'N':>6} {'WinRate':>8} {'Net PnL':>10}")
    print("-" * 40)
    for r in sorted(results, key=lambda x: x["net"], reverse=True)[:15]:
        print(f"{r['entry']:>6} {r['score']:>6} {r['n']:>6} {r['wr']:>7.1f}% ${r['net']:>8.2f}")
    
    # Best combined strategy
    print("\n" + "=" * 70)
    print("BEST COMBINED STRATEGY")
    print("=" * 70)
    
    # Full-set at 94c (best edge without being too rare)
    best_fullset_trades = [o for o in fullset_opps if o[1] <= 94]
    fullset_net = sum(o[2] for o in best_fullset_trades)
    fullset_windows = set(o[0] for o in best_fullset_trades)
    
    # Directional at 93c+ with score <= -1 (best from results)
    best_dir_trades = [o for o in directional_opps 
                       if o[1] >= 93 and o[2] <= -1 
                       and o[0] not in fullset_windows]  # Don't overlap with fullset
    dir_wins = sum(1 for o in best_dir_trades if o[3])
    dir_net = sum(o[4] for o in best_dir_trades)
    
    print(f"\n1. Full-set (combined <= 94c):")
    print(f"   Trades: {len(best_fullset_trades)}")
    print(f"   Net PnL: ${fullset_net:.2f}")
    
    print(f"\n2. Directional (93c+, score <= -1, non-overlapping):")
    print(f"   Trades: {len(best_dir_trades)}")
    print(f"   Win rate: {dir_wins/len(best_dir_trades)*100:.1f}%" if best_dir_trades else "   No trades")
    print(f"   Net PnL: ${dir_net:.2f}")
    
    combined_net = fullset_net + dir_net
    combined_trades = len(best_fullset_trades) + len(best_dir_trades)
    
    print(f"\nCOMBINED:")
    print(f"   Total trades: {combined_trades}")
    print(f"   NET PnL (51 days): ${combined_net:.2f}")
    print(f"   Per day: ${combined_net/51:.2f}")
    print(f"   Per month: ${combined_net/51*30:.2f}")
    print(f"   Per year: ${combined_net/51*365:.2f}")
    print(f"\n   With $1000 capital (100x):")
    print(f"   Per month: ${combined_net/51*30*100:.2f}")
    print(f"   Per year: ${combined_net/51*365*100:.2f}")


if __name__ == "__main__":
    main()

