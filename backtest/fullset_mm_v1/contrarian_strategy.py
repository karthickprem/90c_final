"""
CONTRARIAN STRATEGY: Bet against weak spikes

Idea: When a spike looks weak (high reversal score), 
don't bet on the spike - instead, wait for it to fail 
and buy the OPPOSITE side at a better price.

This is more sophisticated than simple fade.
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
    print("CONTRARIAN STRATEGY: Wait for spike failure")
    print("=" * 70)
    print("""
    Approach:
    1. Detect a spike to 90c+
    2. If spike drops back to 85c or below (failed spike)
    3. Buy the OPPOSITE side (which is now higher)
    4. The failed spike often means opposite will win
    """)
    
    trades = []
    
    for wid in common:
        buy_ticks, sell_ticks = load_window_ticks(wid, DEFAULT_BUY_DIR, DEFAULT_SELL_DIR)
        if len(buy_ticks) < 20 or len(sell_ticks) < 20:
            continue
        
        merged = merge_tick_streams(buy_ticks, sell_ticks)
        if len(merged) < 40:
            continue
        
        final = merged[-1]
        
        # Track spike and failure
        up_peaked = False
        up_peak_price = 0
        down_peaked = False
        down_peak_price = 0
        
        traded_up_fail = False
        traded_down_fail = False
        
        for idx, tick in enumerate(merged):
            t = tick.elapsed_secs
            
            if t < 30 or t > 600:  # Look in middle of window
                continue
            
            # Track UP spikes
            if tick.up_ask >= 90:
                up_peaked = True
                up_peak_price = max(up_peak_price, tick.up_ask)
            
            # Detect UP spike failure
            if up_peaked and not traded_up_fail:
                if tick.up_ask <= up_peak_price - 10:  # Dropped 10c from peak
                    # UP spike failed! Buy DOWN
                    down_price = tick.down_ask
                    
                    if down_price >= 85:  # Only if DOWN is now decent
                        final_down = final.down_ask
                        won = final_down >= 97
                        
                        gross = (100 - down_price) / 100 * 10.0 if won else -down_price / 100 * 10.0
                        fee = polymarket_fee(down_price)
                        
                        trades.append({
                            "wid": wid,
                            "type": "UP_FAIL",
                            "entry_price": down_price,
                            "peak_before_fail": up_peak_price,
                            "drop": up_peak_price - tick.up_ask,
                            "won": won,
                            "net": gross - fee
                        })
                        traded_up_fail = True
            
            # Track DOWN spikes
            if tick.down_ask >= 90:
                down_peaked = True
                down_peak_price = max(down_peak_price, tick.down_ask)
            
            # Detect DOWN spike failure
            if down_peaked and not traded_down_fail:
                if tick.down_ask <= down_peak_price - 10:
                    up_price = tick.up_ask
                    
                    if up_price >= 85:
                        final_up = final.up_ask
                        won = final_up >= 97
                        
                        gross = (100 - up_price) / 100 * 10.0 if won else -up_price / 100 * 10.0
                        fee = polymarket_fee(up_price)
                        
                        trades.append({
                            "wid": wid,
                            "type": "DOWN_FAIL",
                            "entry_price": up_price,
                            "peak_before_fail": down_peak_price,
                            "drop": down_peak_price - tick.down_ask,
                            "won": won,
                            "net": gross - fee
                        })
                        traded_down_fail = True
    
    print(f"\nTotal 'failed spike' trades: {len(trades)}")
    
    if trades:
        wins = sum(1 for t in trades if t["won"])
        net = sum(t["net"] for t in trades)
        
        print(f"Win rate: {wins}/{len(trades)} ({wins/len(trades)*100:.1f}%)")
        print(f"Net PnL: ${net:.2f}")
        print(f"Per trade: ${net/len(trades):.4f}")
        print(f"Per day: ${net/51:.2f}")
        print(f"Per month: ${net/51*30:.2f}")
        
        # By drop size
        print("\n--- By Drop Size ---")
        by_drop = defaultdict(lambda: {"n": 0, "wins": 0, "net": 0})
        for t in trades:
            bucket = t["drop"] // 5 * 5
            by_drop[bucket]["n"] += 1
            if t["won"]:
                by_drop[bucket]["wins"] += 1
            by_drop[bucket]["net"] += t["net"]
        
        print(f"{'Drop':<8} {'N':<8} {'WinRate':<10} {'Net':<10}")
        for drop in sorted(by_drop.keys()):
            r = by_drop[drop]
            if r["n"] < 5:
                continue
            wr = r["wins"] / r["n"] * 100
            print(f"{drop}c+     {r['n']:<8} {wr:>7.1f}%   ${r['net']:.2f}")
        
        # By entry price
        print("\n--- By Entry Price ---")
        by_entry = defaultdict(lambda: {"n": 0, "wins": 0, "net": 0})
        for t in trades:
            by_entry[t["entry_price"]]["n"] += 1
            if t["won"]:
                by_entry[t["entry_price"]]["wins"] += 1
            by_entry[t["entry_price"]]["net"] += t["net"]
        
        print(f"{'Entry':<8} {'N':<8} {'WinRate':<10} {'Net':<10}")
        for entry in sorted(by_entry.keys()):
            r = by_entry[entry]
            if r["n"] < 5:
                continue
            wr = r["wins"] / r["n"] * 100
            print(f"{entry}c     {r['n']:<8} {wr:>7.1f}%   ${r['net']:.2f}")
    
    # Also try: Double reversal (spike, fail, spike again)
    print("\n" + "=" * 70)
    print("DOUBLE REVERSAL PATTERN")
    print("=" * 70)
    print("""
    Pattern: Spike -> Drop -> Recovery
    If we see: 90c -> 80c -> 88c+
    Then: Buy on the recovery (second leg up)
    """)
    
    double_trades = []
    
    for wid in common:
        buy_ticks, sell_ticks = load_window_ticks(wid, DEFAULT_BUY_DIR, DEFAULT_SELL_DIR)
        if len(buy_ticks) < 30 or len(sell_ticks) < 30:
            continue
        
        merged = merge_tick_streams(buy_ticks, sell_ticks)
        if len(merged) < 50:
            continue
        
        final = merged[-1]
        
        for side in ["UP", "DOWN"]:
            # State machine: NONE -> PEAK -> DROP -> RECOVERY
            state = "NONE"
            peak_price = 0
            drop_price = 100
            traded = False
            
            for tick in merged:
                t = tick.elapsed_secs
                price = tick.up_ask if side == "UP" else tick.down_ask
                
                if t > 700:  # Stop looking in last 3 min
                    break
                
                if state == "NONE":
                    if price >= 90:
                        state = "PEAK"
                        peak_price = price
                
                elif state == "PEAK":
                    if price > peak_price:
                        peak_price = price
                    elif price <= peak_price - 8:  # Dropped 8c
                        state = "DROP"
                        drop_price = price
                
                elif state == "DROP":
                    if price < drop_price:
                        drop_price = price
                    elif price >= drop_price + 5 and price >= 88:  # Recovered 5c and at 88+
                        # Recovery! Enter trade
                        final_price = final.up_ask if side == "UP" else final.down_ask
                        won = final_price >= 97
                        
                        gross = (100 - price) / 100 * 10.0 if won else -price / 100 * 10.0
                        fee = polymarket_fee(price)
                        
                        double_trades.append({
                            "wid": wid,
                            "side": side,
                            "peak": peak_price,
                            "drop": drop_price,
                            "recovery_entry": price,
                            "won": won,
                            "net": gross - fee
                        })
                        traded = True
                        break
    
    print(f"\nDouble reversal trades: {len(double_trades)}")
    
    if double_trades:
        wins = sum(1 for t in double_trades if t["won"])
        net = sum(t["net"] for t in double_trades)
        
        print(f"Win rate: {wins}/{len(double_trades)} ({wins/len(double_trades)*100:.1f}%)")
        print(f"Net PnL: ${net:.2f}")
        print(f"Per month: ${net/51*30:.2f}")


if __name__ == "__main__":
    main()

