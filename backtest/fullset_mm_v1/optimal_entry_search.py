"""
OPTIMAL ENTRY SEARCH

Find the best combination of:
- Entry threshold (90c, 92c, 94c...)
- Max reversal score (filter)
"""
from collections import defaultdict

from .parse import find_window_ids, load_window_ticks
from .stream import merge_tick_streams
from .config import DEFAULT_BUY_DIR, DEFAULT_SELL_DIR


def polymarket_fee(price_cents: int, size_dollars: float) -> float:
    """Calculate Polymarket taker fee."""
    if price_cents <= 0 or price_cents >= 100:
        return 0
    p = price_cents / 100.0
    shares = size_dollars / (price_cents / 100.0)
    fee_per_share = 0.25 * (p * (1 - p)) ** 2
    return shares * fee_per_share


def compute_reversal_score(merged, idx, spike_side):
    """Simple reversal score calculation."""
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
    
    # Spike speed
    speed_5s = 0
    for j in range(idx - 1, -1, -1):
        if t - merged[j].elapsed_secs >= 5:
            if spike_side == "UP":
                speed_5s = (spike_price - merged[j].up_ask) / 5
            else:
                speed_5s = (spike_price - merged[j].down_ask) / 5
            break
    
    # Opposite trend
    opp_trend = 0
    for j in range(idx - 1, -1, -1):
        if t - merged[j].elapsed_secs >= 5:
            if spike_side == "UP":
                opp_trend = opp_price - merged[j].down_ask
            else:
                opp_trend = opp_price - merged[j].up_ask
            break
    
    time_remaining = 900 - t
    combined = tick.up_ask + tick.down_ask
    
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
    buy_ids = set(find_window_ids(DEFAULT_BUY_DIR))
    sell_ids = set(find_window_ids(DEFAULT_SELL_DIR))
    common = sorted(buy_ids & sell_ids)
    
    print("=" * 80)
    print("OPTIMAL ENTRY SEARCH")
    print("=" * 80)
    print(f"\nLoading {len(common)} windows...")
    
    # Collect all possible entry points
    entries = []
    
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
        
        # Track first spike per side
        up_recorded = {}  # entry_price -> entry data
        down_recorded = {}
        
        for idx, tick in enumerate(merged):
            t = tick.elapsed_secs
            if t < 10 or t > 570:
                continue
            
            # UP side
            up_price = tick.up_ask
            if up_price >= 88 and up_price not in up_recorded and up_price <= 96:
                score = compute_reversal_score(merged, idx, "UP")
                won = final.up_ask >= 97
                fee = polymarket_fee(up_price, 10.0)
                
                if won:
                    gross = (100 - up_price) / 100 * 10.0
                else:
                    gross = -up_price / 100 * 10.0
                
                up_recorded[up_price] = True
                entries.append({
                    "entry_price": up_price,
                    "score": score,
                    "won": won,
                    "gross": gross,
                    "fee": fee,
                    "net": gross - fee
                })
            
            # DOWN side
            down_price = tick.down_ask
            if down_price >= 88 and down_price not in down_recorded and down_price <= 96:
                score = compute_reversal_score(merged, idx, "DOWN")
                won = final.down_ask >= 97
                fee = polymarket_fee(down_price, 10.0)
                
                if won:
                    gross = (100 - down_price) / 100 * 10.0
                else:
                    gross = -down_price / 100 * 10.0
                
                down_recorded[down_price] = True
                entries.append({
                    "entry_price": down_price,
                    "score": score,
                    "won": won,
                    "gross": gross,
                    "fee": fee,
                    "net": gross - fee
                })
    
    print(f"\nTotal entry opportunities: {len(entries)}")
    
    # Analyze by (entry_price, max_score) combinations
    print("\n" + "=" * 80)
    print("RESULTS: Entry Price x Max Score")
    print("=" * 80)
    print(f"{'Entry':>6} {'MaxScore':>9} {'Trades':>8} {'WinRate':>9} {'Net PnL':>12} {'EV/Trade':>10}")
    print("-" * 60)
    
    best_results = []
    
    for entry_thresh in [88, 90, 91, 92, 93, 94, 95, 96]:
        for max_score in [-1, 0, 1, 2, 3]:
            # Filter entries
            filtered = [e for e in entries 
                       if e["entry_price"] >= entry_thresh 
                       and e["score"] <= max_score]
            
            if len(filtered) < 10:
                continue
            
            n = len(filtered)
            wins = sum(1 for e in filtered if e["won"])
            net = sum(e["net"] for e in filtered)
            ev = net / n
            
            # Only show positive EV results
            result = {
                "entry": entry_thresh,
                "max_score": max_score,
                "trades": n,
                "win_rate": wins/n*100,
                "net": net,
                "ev": ev
            }
            best_results.append(result)
            
            if ev > -0.2:  # Show near-profitable
                print(f"{entry_thresh:>6} {max_score:>9} {n:>8} {wins/n*100:>8.1f}% ${net:>10.2f} ${ev:>9.4f}")
    
    # Top 10 by EV
    print("\n" + "=" * 80)
    print("TOP 10 CONFIGURATIONS BY EV/Trade")
    print("=" * 80)
    print(f"{'Entry':>6} {'MaxScore':>9} {'Trades':>8} {'WinRate':>9} {'Net PnL':>12} {'EV/Trade':>10}")
    print("-" * 60)
    
    for r in sorted(best_results, key=lambda x: x["ev"], reverse=True)[:10]:
        print(f"{r['entry']:>6} {r['max_score']:>9} {r['trades']:>8} {r['win_rate']:>8.1f}% ${r['net']:>10.2f} ${r['ev']:>9.4f}")
    
    # Check full-set opportunities
    print("\n" + "=" * 80)
    print("COMPARISON: Full-set Opportunities")
    print("=" * 80)
    
    fullset_count = 0
    fullset_edge = 0
    
    for i, wid in enumerate(common):
        buy_ticks, sell_ticks = load_window_ticks(wid, DEFAULT_BUY_DIR, DEFAULT_SELL_DIR)
        if len(buy_ticks) < 20 or len(sell_ticks) < 20:
            continue
        
        merged = merge_tick_streams(buy_ticks, sell_ticks)
        if len(merged) < 20:
            continue
        
        for tick in merged:
            combined = tick.up_ask + tick.down_ask
            if combined < 99:
                fullset_count += 1
                edge = 100 - combined
                up_fee = polymarket_fee(tick.up_ask, 10.0)
                down_fee = polymarket_fee(tick.down_ask, 10.0)
                net_edge = edge / 100 * 10.0 * 2 - up_fee - down_fee
                fullset_edge += net_edge
                break
    
    print(f"Windows with full-set opportunity: {fullset_count}")
    print(f"Total net edge: ${fullset_edge:.2f}")
    print(f"Avg net edge per opportunity: ${fullset_edge/fullset_count:.4f}" if fullset_count > 0 else "N/A")


if __name__ == "__main__":
    main()

