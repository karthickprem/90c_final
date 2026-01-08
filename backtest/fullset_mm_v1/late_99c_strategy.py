"""
NEW PROFITABLE STRATEGY: Late 99c Entries

Found: At 99c with 1 minute remaining, win rate is 99.5% and NET PROFITABLE!

Let's explore this in detail.
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
    print("LATE 99c STRATEGY - Deep Analysis")
    print("=" * 70)
    print(f"\nAnalyzing {len(common)} windows...")
    
    # Detailed time buckets
    results = defaultdict(lambda: {"n": 0, "wins": 0, "net": 0})
    
    # Track individual trades for analysis
    all_trades = []
    
    for wid in common:
        buy_ticks, sell_ticks = load_window_ticks(wid, DEFAULT_BUY_DIR, DEFAULT_SELL_DIR)
        if len(buy_ticks) < 20 or len(sell_ticks) < 20:
            continue
        
        merged = merge_tick_streams(buy_ticks, sell_ticks)
        if len(merged) < 20:
            continue
        
        final = merged[-1]
        
        for tick in merged:
            time_remaining = 900 - tick.elapsed_secs
            
            for side in ["UP", "DOWN"]:
                price = tick.up_ask if side == "UP" else tick.down_ask
                
                if price == 99:
                    final_price = final.up_ask if side == "UP" else final.down_ask
                    won = final_price >= 97
                    
                    gross = (100 - 99) / 100 * 10.0 if won else -99 / 100 * 10.0
                    fee = polymarket_fee(99)
                    net = gross - fee
                    
                    # Detailed time bucket (every 10 seconds)
                    time_bucket = int(time_remaining / 10) * 10
                    
                    results[time_bucket]["n"] += 1
                    if won:
                        results[time_bucket]["wins"] += 1
                    results[time_bucket]["net"] += net
                    
                    all_trades.append({
                        "wid": wid,
                        "side": side,
                        "time_remaining": time_remaining,
                        "won": won,
                        "net": net
                    })
    
    print("\n99c ENTRIES BY TIME REMAINING (10-second buckets):")
    print(f"{'Time Left':<12} {'N':<8} {'Wins':<8} {'WinRate':<10} {'Net PnL':<12} {'EV/trade':<10} {'Status'}")
    print("-" * 75)
    
    profitable_windows = []
    
    for time_bucket in sorted(results.keys(), reverse=True):
        r = results[time_bucket]
        if r["n"] < 20:
            continue
        
        wr = r["wins"] / r["n"] * 100
        ev = r["net"] / r["n"]
        status = "PROFIT!" if r["net"] > 0 else "loss"
        
        if r["net"] > 0:
            profitable_windows.append((time_bucket, r))
        
        print(f"{time_bucket}s        {r['n']:<8} {r['wins']:<8} {wr:>7.1f}%   ${r['net']:>9.2f}   ${ev:>8.4f}   {status}")
    
    # Best windows
    print("\n" + "=" * 70)
    print("PROFITABLE TIME WINDOWS")
    print("=" * 70)
    
    if profitable_windows:
        total_profit = sum(r["net"] for _, r in profitable_windows)
        total_trades = sum(r["n"] for _, r in profitable_windows)
        
        print(f"\nProfitable time windows found: {len(profitable_windows)}")
        print(f"Total trades in profitable windows: {total_trades}")
        print(f"Total net profit: ${total_profit:.2f}")
        print(f"Average per trade: ${total_profit/total_trades:.4f}")
        
        print("\nBest windows:")
        for time_bucket, r in sorted(profitable_windows, key=lambda x: x[1]["net"], reverse=True)[:5]:
            print(f"  {time_bucket}s remaining: {r['n']} trades, ${r['net']:.2f} profit")
    
    # Required win rate calculation
    print("\n" + "=" * 70)
    print("MATHEMATICS")
    print("=" * 70)
    
    fee = polymarket_fee(99)
    win_profit = 0.01 * 10.0 - fee  # Win $0.10 at 99c
    loss = 0.99 * 10.0 + fee  # Lose $9.90 at 99c
    required_wr = loss / (win_profit + loss) * 100
    
    print(f"""
At 99c entry:
  Win profit: (100 - 99)/100 * $10 - fee = ${win_profit:.4f}
  Loss:       99/100 * $10 + fee = ${loss:.4f}
  Fee:        ${fee:.6f}
  
  Required win rate to break even: {required_wr:.2f}%
  
This means you need {required_wr:.2f}% win rate to be profitable at 99c.
""")
    
    # Check realistic execution
    print("=" * 70)
    print("REALISTIC EXECUTION ANALYSIS")
    print("=" * 70)
    
    # One trade per window (realistic)
    trades_one_per_window = {}
    
    for t in all_trades:
        wid = t["wid"]
        tr = t["time_remaining"]
        
        # Only trades in profitable time windows (50-90 seconds)
        if 50 <= tr <= 90:
            if wid not in trades_one_per_window or tr < trades_one_per_window[wid]["time_remaining"]:
                trades_one_per_window[wid] = t
    
    if trades_one_per_window:
        trades_list = list(trades_one_per_window.values())
        wins = sum(1 for t in trades_list if t["won"])
        net = sum(t["net"] for t in trades_list)
        
        print(f"\nWith ONE trade per window (50-90s time window):")
        print(f"  Total trades: {len(trades_list)}")
        print(f"  Win rate: {wins}/{len(trades_list)} ({wins/len(trades_list)*100:.1f}%)")
        print(f"  Net PnL: ${net:.2f}")
        print(f"  Per trade: ${net/len(trades_list):.4f}")
        print(f"  Per day (51 days): ${net/51:.2f}")
        print(f"  Per month: ${net/51*30:.2f}")


if __name__ == "__main__":
    main()

