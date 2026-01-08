"""Analyze typical spreads in the BTC 15m data."""
from collections import defaultdict

from .parse import find_window_ids, load_window_ticks
from .stream import merge_tick_streams
from .config import DEFAULT_BUY_DIR, DEFAULT_SELL_DIR


def main():
    buy_ids = set(find_window_ids(DEFAULT_BUY_DIR))
    sell_ids = set(find_window_ids(DEFAULT_SELL_DIR))
    common = sorted(buy_ids & sell_ids)[:500]  # Sample first 500
    
    print("Analyzing spreads...")
    
    all_spreads = []
    spread_at_90_plus = []
    
    for wid in common:
        buy_ticks, sell_ticks = load_window_ticks(wid, DEFAULT_BUY_DIR, DEFAULT_SELL_DIR)
        if len(buy_ticks) < 20 or len(sell_ticks) < 20:
            continue
        
        merged = merge_tick_streams(buy_ticks, sell_ticks)
        
        for tick in merged:
            up_spread = tick.up_ask - tick.up_bid
            down_spread = tick.down_ask - tick.down_bid
            
            all_spreads.append(up_spread)
            all_spreads.append(down_spread)
            
            if tick.up_ask >= 90:
                spread_at_90_plus.append(up_spread)
            if tick.down_ask >= 90:
                spread_at_90_plus.append(down_spread)
    
    print(f"\nAll spreads (sampled): {len(all_spreads)} observations")
    print(f"Mean spread: {sum(all_spreads)/len(all_spreads):.2f}c")
    print(f"Median spread: {sorted(all_spreads)[len(all_spreads)//2]}c")
    
    # Distribution
    by_spread = defaultdict(int)
    for s in all_spreads:
        by_spread[s] += 1
    
    print("\nSpread distribution:")
    for sp in sorted(by_spread.keys())[:20]:
        pct = by_spread[sp] / len(all_spreads) * 100
        print(f"  {sp}c: {pct:.1f}%")
    
    print(f"\n\nSpreads when price >= 90c: {len(spread_at_90_plus)} observations")
    if spread_at_90_plus:
        print(f"Mean: {sum(spread_at_90_plus)/len(spread_at_90_plus):.2f}c")
        print(f"Median: {sorted(spread_at_90_plus)[len(spread_at_90_plus)//2]}c")
    
    # Sample some actual tick data
    print("\n\nSAMPLE TICKS (first window with spike):")
    for wid in common:
        buy_ticks, sell_ticks = load_window_ticks(wid, DEFAULT_BUY_DIR, DEFAULT_SELL_DIR)
        if len(buy_ticks) < 20 or len(sell_ticks) < 20:
            continue
        
        merged = merge_tick_streams(buy_ticks, sell_ticks)
        
        has_spike = any(t.up_ask >= 90 or t.down_ask >= 90 for t in merged)
        if has_spike:
            print(f"\nWindow: {wid}")
            print(f"{'Time':<8} {'UP_ask':>8} {'UP_bid':>8} {'Spread':>8} | {'DN_ask':>8} {'DN_bid':>8} {'Spread':>8}")
            print("-" * 70)
            
            for tick in merged[:30]:
                up_sp = tick.up_ask - tick.up_bid
                dn_sp = tick.down_ask - tick.down_bid
                print(f"{tick.elapsed_secs:<8.1f} {tick.up_ask:>8} {tick.up_bid:>8} {up_sp:>8} | {tick.down_ask:>8} {tick.down_bid:>8} {dn_sp:>8}")
            break


if __name__ == "__main__":
    main()

