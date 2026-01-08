"""
DEEP EXPLORATION: Alternative Algorithms

Let's explore strategies we haven't tried:
1. Late-window entries (last 2-3 min) - higher certainty
2. Momentum confirmation - strong consistent movement
3. Time-of-day patterns - some hours more predictable?
4. Extreme price entries (97c+) - very high probability
5. Mean reversion - fade extreme early spikes
6. Combined signals - multiple conditions must align
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


def analyze_late_entries():
    """
    STRATEGY 1: Late Window Entry
    
    Idea: Enter in the last 2-3 minutes when outcome is more certain.
    At 97c+ with 2 min left, the side almost always wins.
    """
    print("\n" + "=" * 70)
    print("STRATEGY 1: LATE WINDOW ENTRY")
    print("=" * 70)
    print("Hypothesis: Entering in last 2-3 min at high prices = high win rate")
    
    buy_ids = set(find_window_ids(DEFAULT_BUY_DIR))
    sell_ids = set(find_window_ids(DEFAULT_SELL_DIR))
    common = sorted(buy_ids & sell_ids)
    
    # Test different time windows and entry prices
    results = defaultdict(lambda: {"n": 0, "wins": 0, "gross": 0, "fee": 0, "net": 0})
    
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
            
            # Only look at last 3 minutes
            if time_remaining > 180 or time_remaining < 10:
                continue
            
            for side in ["UP", "DOWN"]:
                price = tick.up_ask if side == "UP" else tick.down_ask
                final_price = final.up_ask if side == "UP" else final.down_ask
                
                if price >= 95:
                    time_bucket = f"{int(time_remaining/60)}min"
                    price_bucket = price
                    key = (price_bucket, time_bucket)
                    
                    won = final_price >= 97
                    
                    if won:
                        gross = (100 - price) / 100 * 10.0
                    else:
                        gross = -price / 100 * 10.0
                    
                    fee = polymarket_fee(price)
                    net = gross - fee
                    
                    results[key]["n"] += 1
                    if won:
                        results[key]["wins"] += 1
                    results[key]["gross"] += gross
                    results[key]["fee"] += fee
                    results[key]["net"] += net
    
    print("\nResults (Entry Price, Time Remaining):")
    print(f"{'Price':<8} {'Time':<8} {'N':<8} {'WinRate':<10} {'Net PnL':<12} {'EV/trade':<10}")
    print("-" * 60)
    
    for key in sorted(results.keys(), key=lambda x: (x[0], x[1])):
        r = results[key]
        if r["n"] < 10:
            continue
        wr = r["wins"] / r["n"] * 100
        ev = r["net"] / r["n"]
        status = "+" if r["net"] > 0 else ""
        print(f"{key[0]}c     {key[1]:<8} {r['n']:<8} {wr:>7.1f}%   ${status}{r['net']:<10.2f} ${ev:.4f}")


def analyze_momentum():
    """
    STRATEGY 2: Strong Momentum Confirmation
    
    Idea: Only enter when there's strong, consistent upward movement.
    E.g., 5+ consecutive up-ticks AND price rising 10c+ in last 30 seconds.
    """
    print("\n" + "=" * 70)
    print("STRATEGY 2: STRONG MOMENTUM CONFIRMATION")
    print("=" * 70)
    print("Hypothesis: Strong momentum = higher win probability")
    
    buy_ids = set(find_window_ids(DEFAULT_BUY_DIR))
    sell_ids = set(find_window_ids(DEFAULT_SELL_DIR))
    common = sorted(buy_ids & sell_ids)
    
    results = defaultdict(lambda: {"n": 0, "wins": 0, "net": 0})
    
    for wid in common:
        buy_ticks, sell_ticks = load_window_ticks(wid, DEFAULT_BUY_DIR, DEFAULT_SELL_DIR)
        if len(buy_ticks) < 20 or len(sell_ticks) < 20:
            continue
        
        merged = merge_tick_streams(buy_ticks, sell_ticks)
        if len(merged) < 30:
            continue
        
        final = merged[-1]
        
        for idx in range(30, len(merged)):
            tick = merged[idx]
            t = tick.elapsed_secs
            
            if t < 60 or t > 600:  # Skip first minute and last 5 min
                continue
            
            for side in ["UP", "DOWN"]:
                price = tick.up_ask if side == "UP" else tick.down_ask
                
                if price < 85 or price > 95:
                    continue
                
                # Calculate momentum: price change over last 30 seconds
                price_30s_ago = None
                for j in range(idx - 1, -1, -1):
                    if t - merged[j].elapsed_secs >= 30:
                        price_30s_ago = merged[j].up_ask if side == "UP" else merged[j].down_ask
                        break
                
                if price_30s_ago is None:
                    continue
                
                momentum = price - price_30s_ago
                
                # Count consecutive up ticks
                consec = 0
                for j in range(idx - 1, max(0, idx - 10), -1):
                    p_curr = merged[j+1].up_ask if side == "UP" else merged[j+1].down_ask
                    p_prev = merged[j].up_ask if side == "UP" else merged[j].down_ask
                    if p_curr > p_prev:
                        consec += 1
                    else:
                        break
                
                # Only trade if strong momentum
                if momentum >= 10 and consec >= 5:
                    final_price = final.up_ask if side == "UP" else final.down_ask
                    won = final_price >= 97
                    
                    if won:
                        gross = (100 - price) / 100 * 10.0
                    else:
                        gross = -price / 100 * 10.0
                    
                    fee = polymarket_fee(price)
                    net = gross - fee
                    
                    key = f"mom{momentum//5*5}c_consec{consec}"
                    results[key]["n"] += 1
                    if won:
                        results[key]["wins"] += 1
                    results[key]["net"] += net
    
    print("\nResults (Momentum, Consecutive Upticks):")
    print(f"{'Condition':<25} {'N':<8} {'WinRate':<10} {'Net PnL':<12}")
    print("-" * 55)
    
    for key in sorted(results.keys()):
        r = results[key]
        if r["n"] < 5:
            continue
        wr = r["wins"] / r["n"] * 100
        print(f"{key:<25} {r['n']:<8} {wr:>7.1f}%   ${r['net']:.2f}")


def analyze_extreme_entries():
    """
    STRATEGY 3: Extreme Price Entries (97c+)
    
    Idea: At 97c+, the probability of winning is ~97%+.
    Even with tiny profit per trade, volume makes it work.
    """
    print("\n" + "=" * 70)
    print("STRATEGY 3: EXTREME PRICE ENTRIES (97c+)")
    print("=" * 70)
    print("Hypothesis: At 97c+, probability is so high it overcomes low reward")
    
    buy_ids = set(find_window_ids(DEFAULT_BUY_DIR))
    sell_ids = set(find_window_ids(DEFAULT_SELL_DIR))
    common = sorted(buy_ids & sell_ids)
    
    results = defaultdict(lambda: {"n": 0, "wins": 0, "gross": 0, "fee": 0, "net": 0})
    
    for wid in common:
        buy_ticks, sell_ticks = load_window_ticks(wid, DEFAULT_BUY_DIR, DEFAULT_SELL_DIR)
        if len(buy_ticks) < 20 or len(sell_ticks) < 20:
            continue
        
        merged = merge_tick_streams(buy_ticks, sell_ticks)
        final = merged[-1]
        
        # One trade per side per window
        traded = {"UP": False, "DOWN": False}
        
        for tick in merged:
            t = tick.elapsed_secs
            if t < 60:  # Skip first minute
                continue
            
            for side in ["UP", "DOWN"]:
                if traded[side]:
                    continue
                    
                price = tick.up_ask if side == "UP" else tick.down_ask
                
                if price >= 97:
                    final_price = final.up_ask if side == "UP" else final.down_ask
                    won = final_price >= 97
                    
                    if won:
                        gross = (100 - price) / 100 * 10.0
                    else:
                        gross = -price / 100 * 10.0
                    
                    fee = polymarket_fee(price)
                    
                    results[price]["n"] += 1
                    if won:
                        results[price]["wins"] += 1
                    results[price]["gross"] += gross
                    results[price]["fee"] += fee
                    results[price]["net"] += gross - fee
                    traded[side] = True
    
    print("\nResults by Entry Price:")
    print(f"{'Price':<8} {'N':<8} {'Wins':<8} {'WinRate':<10} {'Gross':<10} {'Fees':<10} {'Net':<10} {'EV':<10}")
    print("-" * 80)
    
    total_n = 0
    total_net = 0
    
    for price in sorted(results.keys()):
        r = results[price]
        wr = r["wins"] / r["n"] * 100 if r["n"] > 0 else 0
        ev = r["net"] / r["n"] if r["n"] > 0 else 0
        total_n += r["n"]
        total_net += r["net"]
        
        print(f"{price}c     {r['n']:<8} {r['wins']:<8} {wr:>7.1f}%   ${r['gross']:>7.2f}   ${r['fee']:>7.2f}   ${r['net']:>7.2f}   ${ev:.4f}")
    
    print("-" * 80)
    print(f"TOTAL:   {total_n:<8}                                              ${total_net:.2f}")
    
    # Calculate required win rate at each price
    print("\n\nBREAK-EVEN ANALYSIS:")
    print(f"{'Price':<8} {'Win Profit':<12} {'Loss':<12} {'Required WR':<12} {'Actual WR':<12} {'Edge?'}")
    print("-" * 65)
    
    for price in [97, 98, 99]:
        if price not in results:
            continue
        r = results[price]
        
        fee = polymarket_fee(price)
        win_profit = (100 - price) / 100 * 10.0 - fee
        loss = price / 100 * 10.0 + fee
        required_wr = loss / (win_profit + loss) * 100
        actual_wr = r["wins"] / r["n"] * 100 if r["n"] > 0 else 0
        edge = "YES!" if actual_wr > required_wr else "NO"
        
        print(f"{price}c     ${win_profit:<10.3f} ${loss:<10.3f} {required_wr:>9.1f}%   {actual_wr:>9.1f}%   {edge}")


def analyze_maker_strategy():
    """
    STRATEGY 4: Maker Orders (Theoretical)
    
    Idea: Instead of paying taker fees, POST orders to earn rebates.
    This is theoretical since we can't test fills from our data.
    """
    print("\n" + "=" * 70)
    print("STRATEGY 4: MAKER ORDERS (Theoretical Analysis)")
    print("=" * 70)
    print("""
The key insight from @0x8dxd's wallet:
- 974,000+ trades
- Mostly MAKER orders (resting limits that get filled)
- Earns rebates instead of paying fees

Maker rebate on Polymarket: ~0.5-1% of trade value
Taker fee we calculated: ~0.6% at mid prices

If we could be a MAKER:
- At 97c: Taker pays ~$0.04 fee, but Maker EARNS ~$0.05 rebate
- This flips the economics entirely!

The problem:
- We can't backtest maker fills accurately
- Our data only shows BBO (best bid/offer), not depth
- Real maker orders might not get filled

CONCLUSION:
If you can consistently get maker fills, even 97-99c entries become profitable.
But this requires:
1. Sophisticated order management
2. Queue position priority
3. Low latency infrastructure
""")


def analyze_combined_signals():
    """
    STRATEGY 5: Combined Signals
    
    Multiple conditions must align:
    - Price >= 96c
    - Time remaining < 3 min
    - Opposite side < 5c
    - Momentum positive
    """
    print("\n" + "=" * 70)
    print("STRATEGY 5: COMBINED SIGNALS (Multiple Conditions)")
    print("=" * 70)
    
    buy_ids = set(find_window_ids(DEFAULT_BUY_DIR))
    sell_ids = set(find_window_ids(DEFAULT_SELL_DIR))
    common = sorted(buy_ids & sell_ids)
    
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
        
        for idx in range(10, len(merged)):
            if traded:
                break
                
            tick = merged[idx]
            t = tick.elapsed_secs
            time_remaining = 900 - t
            
            for side in ["UP", "DOWN"]:
                if traded:
                    break
                    
                price = tick.up_ask if side == "UP" else tick.down_ask
                opp_price = tick.down_ask if side == "UP" else tick.up_ask
                
                # Combined conditions:
                # 1. Price >= 96c
                # 2. Time remaining 60-180 seconds
                # 3. Opposite side <= 5c
                # 4. Price increased in last 10 seconds
                
                if price < 96:
                    continue
                if time_remaining < 60 or time_remaining > 180:
                    continue
                if opp_price > 5:
                    continue
                
                # Check price increase
                prev_price = None
                for j in range(idx - 1, -1, -1):
                    if t - merged[j].elapsed_secs >= 10:
                        prev_price = merged[j].up_ask if side == "UP" else merged[j].down_ask
                        break
                
                if prev_price is None or price <= prev_price:
                    continue
                
                # All conditions met!
                final_price = final.up_ask if side == "UP" else final.down_ask
                won = final_price >= 97
                
                if won:
                    gross = (100 - price) / 100 * 10.0
                else:
                    gross = -price / 100 * 10.0
                
                fee = polymarket_fee(price)
                
                trades.append({
                    "wid": wid,
                    "price": price,
                    "opp": opp_price,
                    "time_left": time_remaining,
                    "won": won,
                    "gross": gross,
                    "fee": fee,
                    "net": gross - fee
                })
                traded = True
    
    print(f"\nTrades meeting ALL conditions: {len(trades)}")
    
    if trades:
        wins = sum(1 for t in trades if t["won"])
        net = sum(t["net"] for t in trades)
        
        print(f"Win rate: {wins}/{len(trades)} ({wins/len(trades)*100:.1f}%)")
        print(f"Net PnL: ${net:.2f}")
        print(f"Avg per trade: ${net/len(trades):.4f}")
        
        print("\nSample trades:")
        for t in trades[:10]:
            print(f"  {t['wid']}: {t['price']}c, opp={t['opp']}c, {t['time_left']:.0f}s left, won={t['won']}, net=${t['net']:.3f}")


def main():
    print("=" * 70)
    print("DEEP EXPLORATION: ALTERNATIVE ALGORITHMS")
    print("=" * 70)
    print("\nSearching for profitable strategies beyond full-set arbitrage...")
    
    # Run all analyses
    analyze_late_entries()
    analyze_momentum()
    analyze_extreme_entries()
    analyze_maker_strategy()
    analyze_combined_signals()
    
    print("\n" + "=" * 70)
    print("FINAL SUMMARY")
    print("=" * 70)


if __name__ == "__main__":
    main()

