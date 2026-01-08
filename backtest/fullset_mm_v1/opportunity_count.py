"""Count all full-set opportunities accurately."""
import sys
sys.stdout.reconfigure(encoding='utf-8')

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
    print("FULL-SET OPPORTUNITY COUNT")
    print("=" * 70)
    print(f"\nAnalyzing {len(common)} windows...")
    
    # Track all opportunities
    by_cost = {}
    windows_with_opp = {}  # cost -> set of windows
    
    for i, wid in enumerate(common):
        if i % 1000 == 0:
            print(f"  {i}/{len(common)}...")
        
        buy_ticks, sell_ticks = load_window_ticks(wid, DEFAULT_BUY_DIR, DEFAULT_SELL_DIR)
        if len(buy_ticks) < 5 or len(sell_ticks) < 5:
            continue
        
        merged = merge_tick_streams(buy_ticks, sell_ticks)
        
        for tick in merged:
            combined = tick.up_ask + tick.down_ask
            if combined < 100:
                by_cost[combined] = by_cost.get(combined, 0) + 1
                if combined not in windows_with_opp:
                    windows_with_opp[combined] = set()
                windows_with_opp[combined].add(wid)
    
    # Calculate profits
    print("\n" + "=" * 70)
    print("RESULTS BY COMBINED COST")
    print("=" * 70)
    print(f"\n{'Cost':<6} {'Ticks':<8} {'Windows':<10} {'Edge':<6} {'Fee':<8} {'Net':<8} {'Status':<10}")
    print("-" * 65)
    
    total_ticks = 0
    profitable_ticks = 0
    total_windows = set()
    profitable_windows = set()
    total_profit = 0
    
    for cost in sorted(by_cost.keys()):
        count = by_cost[cost]
        num_windows = len(windows_with_opp[cost])
        
        edge = 100 - cost
        gross = edge / 100 * 10.0 * 2  # $10 per leg
        
        # Fee calculation
        up_price = cost // 2
        down_price = cost - up_price
        fee = polymarket_fee(up_price) + polymarket_fee(down_price)
        net = gross - fee
        
        status = "PROFIT" if net > 0 else "LOSS"
        
        total_ticks += count
        total_windows.update(windows_with_opp[cost])
        
        if net > 0:
            profitable_ticks += count
            profitable_windows.update(windows_with_opp[cost])
            total_profit += net * count
        
        print(f"{cost}c    {count:<8} {num_windows:<10} {edge}c    ${fee:<7.2f} ${net:<7.2f} {status}")
    
    print("-" * 65)
    
    print(f"\n" + "=" * 70)
    print("SUMMARY")
    print("=" * 70)
    print(f"""
TOTAL FULL-SET OPPORTUNITIES (combined < 100c):

  Tick-level:     {total_ticks:,} moments
  Unique windows: {len(total_windows):,} ({len(total_windows)/len(common)*100:.1f}% of all windows)

PROFITABLE AFTER FEES (combined <= 96c):

  Tick-level:     {profitable_ticks:,} moments
  Unique windows: {len(profitable_windows):,} ({len(profitable_windows)/len(common)*100:.1f}% of all windows)
  Total profit:   ${total_profit:,.2f}

FREQUENCY (over 51 days):

  All opportunities:        ~{total_ticks/51:.0f} ticks/day
  Profitable opportunities: ~{profitable_ticks/51:.0f} ticks/day
  Windows with opportunity: ~{len(total_windows)/51:.0f}/day

REALITY CHECK:
  - Most opportunities are at 99c (2,754 ticks) - NOT profitable
  - Only {profitable_ticks/total_ticks*100:.1f}% of opportunities are profitable after fees
  - Need combined <= 96c to make money
""")


if __name__ == "__main__":
    main()

