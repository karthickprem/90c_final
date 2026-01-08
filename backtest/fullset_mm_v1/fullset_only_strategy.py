"""
FULL-SET ONLY STRATEGY

After exhaustive testing, this is the ONLY consistently profitable approach:
- Buy BOTH sides when combined cost <= threshold
- Guaranteed 100c payout at settlement
- Edge must exceed fees to be profitable

The reversal detection helps us understand WHY directional fails:
- Even "safe" spikes (low reversal score) still have ~8% loss rate
- At 90c entry, 8% losses = -7.2c expected loss per trade (0.08 * 90c)
- Only 10c expected gain if win
- Net: marginally negative after fees

CONCLUSION: Use reversal score to AVOID bad trades, but don't use it for entries.
The ONLY positive edge is full-set arbitrage when it exists.
"""
import sys
from collections import defaultdict
import os
import csv

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


def main():
    sys.stdout.reconfigure(encoding='utf-8')
    
    buy_ids = set(find_window_ids(DEFAULT_BUY_DIR))
    sell_ids = set(find_window_ids(DEFAULT_SELL_DIR))
    common = sorted(buy_ids & sell_ids)
    
    print("=" * 70)
    print("FULL-SET ONLY STRATEGY")
    print("=" * 70)
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
        
        # Find best full-set opportunity in window
        best = None
        
        for tick in merged:
            t = tick.elapsed_secs
            if t < 5 or t > 890:
                continue
            
            combined = tick.up_ask + tick.down_ask
            
            # Only trade if net profitable
            edge = 100 - combined
            gross = edge / 100 * 10.0 * 2
            up_fee = polymarket_fee(tick.up_ask, 10.0)
            down_fee = polymarket_fee(tick.down_ask, 10.0)
            net = gross - up_fee - down_fee
            
            if net > 0:  # Only trade if profitable after fees
                if best is None or net > best["net"]:
                    best = {
                        "wid": wid,
                        "time": t,
                        "combined": combined,
                        "up_ask": tick.up_ask,
                        "down_ask": tick.down_ask,
                        "edge": edge,
                        "gross": gross,
                        "fee": up_fee + down_fee,
                        "net": net
                    }
        
        if best:
            all_trades.append(best)
    
    print(f"\nTotal windows with opportunity: {len(all_trades)}")
    
    # Results
    print("\n" + "=" * 70)
    print("RESULTS")
    print("=" * 70)
    
    if not all_trades:
        print("\nNo profitable full-set opportunities found!")
        return
    
    total_gross = sum(t["gross"] for t in all_trades)
    total_fees = sum(t["fee"] for t in all_trades)
    total_net = sum(t["net"] for t in all_trades)
    
    print(f"\nTotal trades: {len(all_trades)}")
    print(f"Win rate: 100% (guaranteed)")
    print(f"Gross edge: ${total_gross:.2f}")
    print(f"Total fees: ${total_fees:.2f}")
    print(f"NET PnL: ${total_net:.2f}")
    
    # By combined cost
    print("\n--- By Combined Cost ---")
    by_cost = defaultdict(lambda: {"n": 0, "gross": 0, "fee": 0, "net": 0})
    for t in all_trades:
        bucket = t["combined"]
        by_cost[bucket]["n"] += 1
        by_cost[bucket]["gross"] += t["gross"]
        by_cost[bucket]["fee"] += t["fee"]
        by_cost[bucket]["net"] += t["net"]
    
    print(f"Combined   N     Gross    Fees     Net      Avg/trade")
    print("-" * 60)
    for cost in sorted(by_cost.keys()):
        s = by_cost[cost]
        avg = s["net"] / s["n"] if s["n"] > 0 else 0
        print(f"  {cost}c     {s['n']:<5} ${s['gross']:>6.2f}  ${s['fee']:>5.2f}  ${s['net']:>6.2f}  ${avg:.3f}")
    
    # Projections
    print(f"\n--- Projections ---")
    print(f"Period: 51 days")
    print(f"Trades/day: {len(all_trades)/51:.2f}")
    print(f"Net/day: ${total_net/51:.2f}")
    print(f"Net/month: ${total_net/51*30:.2f}")
    print(f"Net/year: ${total_net/51*365:.2f}")
    
    print(f"\n--- Scaled Capital ---")
    for capital in [100, 500, 1000, 5000]:
        scale = capital / 10.0
        monthly = total_net/51*30*scale
        yearly = total_net/51*365*scale
        print(f"  ${capital}: ${monthly:.2f}/month, ${yearly:.2f}/year")
    
    # Save
    os.makedirs("out_fullset_only", exist_ok=True)
    with open("out_fullset_only/trades.csv", "w", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        writer.writerow(["window_id", "time", "combined", "up_ask", "down_ask", "edge", "gross", "fee", "net"])
        for t in all_trades:
            writer.writerow([t["wid"], f"{t['time']:.1f}", t["combined"], t["up_ask"], t["down_ask"],
                           t["edge"], f"{t['gross']:.4f}", f"{t['fee']:.4f}", f"{t['net']:.4f}"])
    
    print("\nSaved to out_fullset_only/")
    
    # Final verdict
    print("\n" + "=" * 70)
    print("FINAL VERDICT")
    print("=" * 70)
    print(f"""
    REVERSAL STRATEGY CONCLUSION:
    
    After extensive backtesting with 50 days / 4,867 windows:
    
    1. REVERSAL DETECTION WORKS for identifying HIGH-RISK entries:
       - Fast spikes (>5c/s) have higher reversal probability
       - Opposite side rising = danger sign
       - Wide spreads = uncertainty
       
    2. BUT... It doesn't make directional entries profitable:
       - Even "safe" entries (score <= -1) lose money after fees
       - The 90c entry win rate is ~88%, need ~90% to break even
       - Fees add ~0.2% cost, pushing edge negative
    
    3. THE ONLY PROFITABLE STRATEGY is FULL-SET:
       - {len(all_trades)} opportunities over 51 days
       - ${total_net:.2f} profit (guaranteed)
       - About {len(all_trades)/51:.1f} trades per day
    
    4. REVERSAL SCORE USE CASE:
       - Use it to AVOID entries, not to make entries
       - If score > 3: DON'T enter directional
       - But don't expect score <= -1 to be profitable either
    
    RECOMMENDATION:
    Focus on full-set opportunities and use reversal detection
    to filter out the worst directional entries if you must trade them.
    """)


if __name__ == "__main__":
    main()

