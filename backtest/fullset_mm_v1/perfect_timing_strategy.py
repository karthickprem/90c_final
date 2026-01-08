"""
PERFECT TIMING STRATEGY

Found: At 99c with specific time remaining, we get 100% win rate!
- 30 seconds left: 100% (613 trades)
- 90-110 seconds left: 100% (1323 trades)

Let's build a realistic strategy around this.
"""
import sys
sys.stdout.reconfigure(encoding='utf-8')

from collections import defaultdict
from .parse import find_window_ids, load_window_ticks
from .stream import merge_tick_streams
from .config import DEFAULT_BUY_DIR, DEFAULT_SELL_DIR


def polymarket_fee(price_cents, size=10.0):
    if price_cents <= 0 or price_cents >= 100:
        return 0
    p = price_cents / 100.0
    shares = size / p
    return shares * 0.25 * (p * (1 - p)) ** 2


def main():
    buy_ids = set(find_window_ids(DEFAULT_BUY_DIR))
    sell_ids = set(find_window_ids(DEFAULT_SELL_DIR))
    common = sorted(buy_ids & sell_ids)
    
    print("=" * 70)
    print("PERFECT TIMING STRATEGY")
    print("=" * 70)
    
    # Strategy: Buy at 99c when time remaining is 25-35s or 85-115s
    # (These showed 100% win rate in tick-level analysis)
    
    target_windows = [
        (25, 35, "30s window"),
        (85, 115, "90-110s window"),
        (105, 115, "110s window"),
        (25, 115, "combined")
    ]
    
    for time_min, time_max, label in target_windows:
        print(f"\n--- {label} ({time_min}-{time_max}s remaining) ---")
        
        trades = []
        
        for wid in common:
            buy_ticks, sell_ticks = load_window_ticks(wid, DEFAULT_BUY_DIR, DEFAULT_SELL_DIR)
            if len(buy_ticks) < 20 or len(sell_ticks) < 20:
                continue
            
            merged = merge_tick_streams(buy_ticks, sell_ticks)
            if len(merged) < 20:
                continue
            
            final = merged[-1]
            traded = False
            
            for tick in merged:
                if traded:
                    break
                
                time_remaining = 900 - tick.elapsed_secs
                
                if time_min <= time_remaining <= time_max:
                    for side in ["UP", "DOWN"]:
                        price = tick.up_ask if side == "UP" else tick.down_ask
                        
                        if price == 99:
                            final_price = final.up_ask if side == "UP" else final.down_ask
                            won = final_price >= 97
                            
                            gross = 0.01 * 10.0 if won else -0.99 * 10.0
                            fee = polymarket_fee(99)
                            net = gross - fee
                            
                            trades.append({
                                "wid": wid,
                                "side": side,
                                "time": time_remaining,
                                "won": won,
                                "net": net
                            })
                            traded = True
                            break
        
        if trades:
            wins = sum(1 for t in trades if t["won"])
            net = sum(t["net"] for t in trades)
            
            print(f"  Trades: {len(trades)}")
            print(f"  Win rate: {wins}/{len(trades)} ({wins/len(trades)*100:.2f}%)")
            print(f"  Net PnL: ${net:.2f}")
            print(f"  Per trade: ${net/len(trades):.4f}")
            print(f"  Per day: ${net/51:.2f}")
            print(f"  Per month: ${net/51*30:.2f}")
    
    # Now let's try different price levels with tight time windows
    print("\n" + "=" * 70)
    print("EXPLORING OTHER PRICES AT OPTIMAL TIMES")
    print("=" * 70)
    
    for price_thresh in [97, 98, 99, 100]:
        print(f"\n--- Price = {price_thresh}c, Time = 25-35s ---")
        
        trades = []
        
        for wid in common:
            buy_ticks, sell_ticks = load_window_ticks(wid, DEFAULT_BUY_DIR, DEFAULT_SELL_DIR)
            if len(buy_ticks) < 20 or len(sell_ticks) < 20:
                continue
            
            merged = merge_tick_streams(buy_ticks, sell_ticks)
            if len(merged) < 20:
                continue
            
            final = merged[-1]
            traded = False
            
            for tick in merged:
                if traded:
                    break
                
                time_remaining = 900 - tick.elapsed_secs
                
                if 25 <= time_remaining <= 35:
                    for side in ["UP", "DOWN"]:
                        price = tick.up_ask if side == "UP" else tick.down_ask
                        
                        if price >= price_thresh:
                            final_price = final.up_ask if side == "UP" else final.down_ask
                            won = final_price >= 97
                            
                            gross = (100 - price) / 100 * 10.0 if won else -price / 100 * 10.0
                            fee = polymarket_fee(price)
                            net = gross - fee
                            
                            trades.append({
                                "wid": wid,
                                "price": price,
                                "won": won,
                                "net": net
                            })
                            traded = True
                            break
        
        if trades:
            wins = sum(1 for t in trades if t["won"])
            net = sum(t["net"] for t in trades)
            
            print(f"  Trades: {len(trades)}")
            print(f"  Win rate: {wins}/{len(trades)} ({wins/len(trades)*100:.2f}%)")
            print(f"  Net PnL: ${net:.2f}")
            print(f"  Per month: ${net/51*30:.2f}")
    
    # Final: Combined strategy
    print("\n" + "=" * 70)
    print("FINAL COMBINED STRATEGY")
    print("=" * 70)
    print("""
    Best approach found:
    
    1. FULL-SET when combined <= 96c (anywhere in window)
       - ~150 windows, ~$145 profit
    
    2. DIRECTIONAL at 99c with 25-35s or 85-115s remaining
       - ~100% win rate in certain time windows
       - Small profit per trade but very high frequency
    
    3. MAKER ORDERS (theoretical)
       - If you can get maker fills, economics flip
       - Earn rebates instead of paying fees
    """)
    
    # Calculate combined
    print("\nCOMBINED ESTIMATE:")
    print("-" * 40)
    
    # Full-set from earlier: $145 over 51 days
    fullset_profit = 145.22
    
    # 99c late entries (from 25-35s window test)
    # Need to re-run to get exact number
    late_trades = []
    for wid in common:
        buy_ticks, sell_ticks = load_window_ticks(wid, DEFAULT_BUY_DIR, DEFAULT_SELL_DIR)
        if len(buy_ticks) < 20 or len(sell_ticks) < 20:
            continue
        
        merged = merge_tick_streams(buy_ticks, sell_ticks)
        if len(merged) < 20:
            continue
        
        final = merged[-1]
        traded = False
        
        for tick in merged:
            if traded:
                break
            
            time_remaining = 900 - tick.elapsed_secs
            
            if 25 <= time_remaining <= 35:
                for side in ["UP", "DOWN"]:
                    price = tick.up_ask if side == "UP" else tick.down_ask
                    
                    if price == 99:
                        final_price = final.up_ask if side == "UP" else final.down_ask
                        won = final_price >= 97
                        
                        gross = 0.01 * 10.0 if won else -0.99 * 10.0
                        fee = polymarket_fee(99)
                        net = gross - fee
                        
                        late_trades.append({"won": won, "net": net})
                        traded = True
                        break
    
    late_wins = sum(1 for t in late_trades if t["won"])
    late_net = sum(t["net"] for t in late_trades)
    
    print(f"Full-set (96c threshold): ${fullset_profit:.2f}")
    print(f"Late 99c (25-35s): {len(late_trades)} trades, {late_wins/len(late_trades)*100:.1f}% WR, ${late_net:.2f}")
    print(f"COMBINED: ${fullset_profit + late_net:.2f}")
    print(f"Per day: ${(fullset_profit + late_net)/51:.2f}")
    print(f"Per month: ${(fullset_profit + late_net)/51*30:.2f}")


if __name__ == "__main__":
    main()

