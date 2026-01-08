"""
VERIFY: 95c+ entry with score <= 1

This showed $1,166 profit over 51 days with 10,730 trades!
Let's verify this is real and not a bug.
"""
import sys
from collections import defaultdict

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
    print("VERIFY: 95c+ Entry Strategy")
    print("=" * 70)
    
    # Run the actual strategy (one trade per side per window)
    trades = []
    
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
        
        # Only ONE trade per window (first opportunity)
        traded = False
        
        for idx, tick in enumerate(merged):
            if traded:
                break
            
            t = tick.elapsed_secs
            if t < 10 or t > 570:
                continue
            
            for side in ["UP", "DOWN"]:
                price = tick.up_ask if side == "UP" else tick.down_ask
                
                if price >= 95:
                    score = compute_reversal_score(merged, idx, side)
                    if score <= 1:
                        final_price = final.up_ask if side == "UP" else final.down_ask
                        won = final_price >= 97
                        
                        if won:
                            gross = (100 - price) / 100 * 10.0
                        else:
                            gross = -price / 100 * 10.0
                        
                        fee = polymarket_fee(price, 10.0)
                        net = gross - fee
                        
                        trades.append({
                            "wid": wid,
                            "side": side,
                            "price": price,
                            "score": score,
                            "won": won,
                            "gross": gross,
                            "fee": fee,
                            "net": net
                        })
                        traded = True
                        break
    
    print(f"\nTotal trades (ONE per window): {len(trades)}")
    
    wins = sum(1 for t in trades if t["won"])
    gross = sum(t["gross"] for t in trades)
    fees = sum(t["fee"] for t in trades)
    net = sum(t["net"] for t in trades)
    
    print(f"Win/Loss: {wins}/{len(trades)-wins} ({wins/len(trades)*100:.1f}%)")
    print(f"Gross: ${gross:.2f}")
    print(f"Fees: ${fees:.2f}")
    print(f"NET: ${net:.2f}")
    
    # By entry price
    print("\n--- By Entry Price ---")
    by_price = defaultdict(lambda: {"n": 0, "wins": 0, "net": 0})
    for t in trades:
        by_price[t["price"]]["n"] += 1
        if t["won"]:
            by_price[t["price"]]["wins"] += 1
        by_price[t["price"]]["net"] += t["net"]
    
    print(f"Price   N      Wins    WinRate   Net")
    for p in sorted(by_price.keys()):
        s = by_price[p]
        wr = s["wins"]/s["n"]*100 if s["n"] > 0 else 0
        print(f"{p}c    {s['n']:<6} {s['wins']:<6} {wr:>5.1f}%    ${s['net']:.2f}")
    
    # Projections
    print(f"\n--- Projections ---")
    print(f"Period: 51 days")
    print(f"Trades/day: {len(trades)/51:.1f}")
    print(f"Net/day: ${net/51:.2f}")
    print(f"Net/month: ${net/51*30:.2f}")
    print(f"Net/year: ${net/51*365:.2f}")
    
    print(f"\n--- With $1000 capital (100x sizing) ---")
    print(f"Monthly: ${net/51*30*100:.2f}")
    print(f"Yearly: ${net/51*365*100:.2f}")
    
    # The issue: we had 10730 trades in the earlier run but only ONE per window here
    # That means the earlier run was counting EVERY tick as a trade, not one per window
    print("\n" + "=" * 70)
    print("NOTE")
    print("=" * 70)
    print("""
    The earlier comparison showed 10,730 trades - that was counting
    EVERY qualifying tick, not realistic one-per-window trading.
    
    With realistic ONE trade per window, the numbers are different.
    """)


if __name__ == "__main__":
    main()

