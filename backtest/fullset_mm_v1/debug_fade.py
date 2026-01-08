"""Debug: Why is backtest win rate lower than analysis?"""
from collections import defaultdict

from .parse import find_window_ids, load_window_ticks
from .stream import merge_tick_streams
from .config import DEFAULT_BUY_DIR, DEFAULT_SELL_DIR


def main():
    buy_ids = set(find_window_ids(DEFAULT_BUY_DIR))
    sell_ids = set(find_window_ids(DEFAULT_SELL_DIR))
    common = sorted(buy_ids & sell_ids)
    
    print("Debugging spike outcomes...")
    print()
    
    # Track all spikes with opposite < 15c
    all_spikes = []
    
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
        
        up_spiked = False
        down_spiked = False
        
        for tick in merged:
            t = tick.elapsed_secs
            if t < 10 or t > 870:
                continue
            
            # UP spike
            if not up_spiked and tick.up_ask >= 90:
                up_spiked = True
                opp_price = tick.down_ask
                
                # Final outcome: which side settles at 97+?
                up_wins = final.up_ask >= 97
                down_wins = final.down_ask >= 97
                
                all_spikes.append({
                    "spike_side": "UP",
                    "spike_price": tick.up_ask,
                    "opp_price": opp_price,
                    "final_spike": final.up_ask,
                    "final_opp": final.down_ask,
                    "spike_wins": up_wins,
                    "opp_wins": down_wins
                })
            
            # DOWN spike
            if not down_spiked and tick.down_ask >= 90:
                down_spiked = True
                opp_price = tick.up_ask
                
                up_wins = final.up_ask >= 97
                down_wins = final.down_ask >= 97
                
                all_spikes.append({
                    "spike_side": "DOWN",
                    "spike_price": tick.down_ask,
                    "opp_price": opp_price,
                    "final_spike": final.down_ask,
                    "final_opp": final.up_ask,
                    "spike_wins": down_wins,
                    "opp_wins": up_wins
                })
    
    print(f"\nTotal spikes: {len(all_spikes)}")
    
    # Analyze by opposite price
    print("\nOUTCOME BY OPPOSITE PRICE AT SPIKE")
    print("=" * 70)
    
    by_opp = defaultdict(lambda: {"n": 0, "spike_wins": 0, "opp_wins": 0, "neither": 0})
    
    for s in all_spikes:
        bucket = min(s["opp_price"], 50)
        by_opp[bucket]["n"] += 1
        if s["spike_wins"]:
            by_opp[bucket]["spike_wins"] += 1
        if s["opp_wins"]:
            by_opp[bucket]["opp_wins"] += 1
        if not s["spike_wins"] and not s["opp_wins"]:
            by_opp[bucket]["neither"] += 1
    
    print(f"OppPrice   N       SpikeWins   OppWins   Neither   OppRate")
    print("-" * 70)
    
    for price in sorted(by_opp.keys())[:20]:
        s = by_opp[price]
        opp_rate = s["opp_wins"] / s["n"] * 100 if s["n"] > 0 else 0
        print(f"{price}c        {s['n']:<7} {s['spike_wins']:<10} {s['opp_wins']:<9} {s['neither']:<9} {opp_rate:.1f}%")
    
    # Calculate expected EV for "buy opposite at opp_price"
    print("\n\nEV CALCULATION (at $10 size)")
    print("=" * 70)
    print("If OppWins: profit = (100 - opp_price) * $0.10")
    print("If SpikeWins: loss = opp_price * $0.10")
    print()
    
    print(f"OppPrice   N       OppWins%   ExpectedEV (pre-fee)")
    print("-" * 50)
    
    for price in sorted(by_opp.keys())[:15]:
        s = by_opp[price]
        if s["n"] < 10:
            continue
        opp_rate = s["opp_wins"] / s["n"]
        # EV = prob_win * win_amount - prob_lose * lose_amount
        win_amt = (100 - price) * 0.10
        lose_amt = price * 0.10
        ev = opp_rate * win_amt - (1 - opp_rate) * lose_amt
        print(f"{price}c        {s['n']:<7} {opp_rate*100:>5.1f}%     ${ev:.3f}/trade")
    
    # Total check
    filtered = [s for s in all_spikes if s["opp_price"] <= 12]
    print(f"\n\nFILTERED (opp <= 12c): {len(filtered)} spikes")
    opp_wins = sum(1 for s in filtered if s["opp_wins"])
    print(f"Opposite wins: {opp_wins} ({opp_wins/len(filtered)*100:.1f}%)")
    
    # Calculate theoretical net
    total_gross = 0
    for s in filtered:
        if s["opp_wins"]:
            total_gross += (100 - s["opp_price"]) * 0.10
        else:
            total_gross -= s["opp_price"] * 0.10
    
    print(f"Gross PnL (no fees): ${total_gross:.2f}")
    print(f"Avg per trade: ${total_gross/len(filtered):.4f}")


if __name__ == "__main__":
    main()

