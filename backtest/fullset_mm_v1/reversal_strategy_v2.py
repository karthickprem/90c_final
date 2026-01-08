"""
REVERSAL STRATEGY V2 - FIXED

Key insight from data:
- When spike crosses 90c, opposite side is cheap (~10c)
- Opposite wins 12-32% of the time (depending on reversal score)
- At 10c entry: need only 10% opp win rate to break even
- EVERY reversal score shows >10% opp win rate = PROFITABLE!

Strategy: Buy opposite side ALWAYS when spike crosses 90c
Higher reversal scores = better EV
"""
from dataclasses import dataclass, field
from typing import List, Dict, Optional
from collections import defaultdict
import os
import csv

from .parse import find_window_ids, load_window_ticks
from .stream import merge_tick_streams, QuoteTick
from .config import DEFAULT_BUY_DIR, DEFAULT_SELL_DIR


def polymarket_fee(price_cents: int, size_dollars: float) -> float:
    """Calculate Polymarket taker fee."""
    if price_cents <= 0 or price_cents >= 100:
        return 0
    p = price_cents / 100.0
    shares = size_dollars / (price_cents / 100.0)
    fee_per_share = 0.25 * (p * (1 - p)) ** 2
    return shares * fee_per_share


@dataclass
class FadeTrade:
    """A fade trade (buy opposite when spike occurs)."""
    window_id: str
    
    # Spike info
    spike_side: str
    spike_price: int
    spike_time: float
    
    # Our entry (opposite side)
    entry_side: str
    entry_price: int
    
    # Outcome
    we_win: bool  # Did opposite side win?
    final_price: int
    
    # PnL
    gross_pnl: float  # In dollars
    fee: float
    net_pnl: float


def run_fade_backtest(
    spike_threshold: int = 90,
    size_per_trade: float = 10.0,
    max_opp_price: int = 15  # Only enter if opposite is cheap
) -> List[FadeTrade]:
    """
    Simple strategy: Every time a spike crosses threshold,
    buy the opposite side if it's cheap enough.
    """
    buy_ids = set(find_window_ids(DEFAULT_BUY_DIR))
    sell_ids = set(find_window_ids(DEFAULT_SELL_DIR))
    common = sorted(buy_ids & sell_ids)
    
    print("=" * 70)
    print("FADE STRATEGY BACKTEST V2")
    print("=" * 70)
    print(f"\nConfig:")
    print(f"  Spike threshold: {spike_threshold}c")
    print(f"  Max opposite price to enter: {max_opp_price}c")
    print(f"  Size per trade: ${size_per_trade}")
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
        
        final = merged[-1]
        
        # Track if we've traded this window
        up_spiked = False
        down_spiked = False
        
        for tick in merged:
            t = tick.elapsed_secs
            
            # Skip very early or late
            if t < 10 or t > 870:
                continue
            
            # Check for UP spike
            if not up_spiked and tick.up_ask >= spike_threshold:
                up_spiked = True
                
                # Opposite is DOWN
                opp_price = tick.down_ask
                if opp_price <= max_opp_price:
                    # Trade!
                    we_win = final.down_ask >= 97
                    
                    if we_win:
                        gross = (100 - opp_price) / 100 * size_per_trade
                    else:
                        gross = -opp_price / 100 * size_per_trade
                    
                    fee = polymarket_fee(opp_price, size_per_trade)
                    net = gross - fee
                    
                    trade = FadeTrade(
                        window_id=wid,
                        spike_side="UP",
                        spike_price=tick.up_ask,
                        spike_time=t,
                        entry_side="DOWN",
                        entry_price=opp_price,
                        we_win=we_win,
                        final_price=final.down_ask,
                        gross_pnl=gross,
                        fee=fee,
                        net_pnl=net
                    )
                    all_trades.append(trade)
            
            # Check for DOWN spike
            if not down_spiked and tick.down_ask >= spike_threshold:
                down_spiked = True
                
                opp_price = tick.up_ask
                if opp_price <= max_opp_price:
                    we_win = final.up_ask >= 97
                    
                    if we_win:
                        gross = (100 - opp_price) / 100 * size_per_trade
                    else:
                        gross = -opp_price / 100 * size_per_trade
                    
                    fee = polymarket_fee(opp_price, size_per_trade)
                    net = gross - fee
                    
                    trade = FadeTrade(
                        window_id=wid,
                        spike_side="DOWN",
                        spike_price=tick.down_ask,
                        spike_time=t,
                        entry_side="UP",
                        entry_price=opp_price,
                        we_win=we_win,
                        final_price=final.up_ask,
                        gross_pnl=gross,
                        fee=fee,
                        net_pnl=net
                    )
                    all_trades.append(trade)
    
    return all_trades


def analyze_trades(trades: List[FadeTrade], days: int = 51):
    """Analyze fade trade results."""
    print("\n" + "=" * 70)
    print("RESULTS")
    print("=" * 70)
    
    if not trades:
        print("No trades!")
        return
    
    n = len(trades)
    wins = sum(1 for t in trades if t.we_win)
    losses = n - wins
    win_rate = wins / n * 100
    
    gross_total = sum(t.gross_pnl for t in trades)
    fee_total = sum(t.fee for t in trades)
    net_total = sum(t.net_pnl for t in trades)
    
    avg_entry = sum(t.entry_price for t in trades) / n
    
    print(f"\nTotal trades: {n}")
    print(f"Win/Loss: {wins}/{losses} ({win_rate:.1f}%)")
    print(f"Avg entry price: {avg_entry:.1f}c")
    
    print(f"\nGross PnL: ${gross_total:.2f}")
    print(f"Total fees: ${fee_total:.2f}")
    print(f"NET PnL: ${net_total:.2f}")
    
    if n > 0:
        print(f"Avg net PnL/trade: ${net_total/n:.4f}")
    
    print(f"\n--- Projection ---")
    print(f"PnL over {days} days: ${net_total:.2f}")
    print(f"PnL per day: ${net_total/days:.2f}")
    print(f"PnL per 30 days: ${net_total/days*30:.2f}")
    print(f"Annual: ${net_total/days*365:.2f}")
    
    # Break down by entry price
    print("\n--- By Entry Price ---")
    by_price = defaultdict(lambda: {"n": 0, "wins": 0, "gross": 0, "fee": 0, "net": 0})
    
    for t in trades:
        bucket = (t.entry_price // 3) * 3
        by_price[bucket]["n"] += 1
        if t.we_win:
            by_price[bucket]["wins"] += 1
        by_price[bucket]["gross"] += t.gross_pnl
        by_price[bucket]["fee"] += t.fee
        by_price[bucket]["net"] += t.net_pnl
    
    print(f"Entry    N       Wins    WinRate   Gross       Fees      Net")
    print("-" * 70)
    for price in sorted(by_price.keys()):
        s = by_price[price]
        wr = s["wins"] / s["n"] * 100 if s["n"] > 0 else 0
        print(f"{price}c      {s['n']:<7} {s['wins']:<7} {wr:>5.1f}%    ${s['gross']:>7.2f}   ${s['fee']:>5.2f}   ${s['net']:>7.2f}")


def run_sensitivity():
    """Test different entry thresholds."""
    print("=" * 70)
    print("SENSITIVITY: Entry Price Threshold")
    print("=" * 70)
    
    results = []
    
    for max_opp in [5, 8, 10, 12, 15, 20]:
        print(f"\n--- Max opposite price: {max_opp}c ---")
        trades = run_fade_backtest(90, 10.0, max_opp)
        
        if trades:
            n = len(trades)
            wins = sum(1 for t in trades if t.we_win)
            net = sum(t.net_pnl for t in trades)
            
            results.append({
                "max_opp": max_opp,
                "trades": n,
                "win_rate": wins/n*100,
                "net_pnl": net
            })
    
    print("\n" + "=" * 70)
    print("SUMMARY")
    print("=" * 70)
    print(f"MaxOpp   Trades   WinRate   Net PnL   $/trade")
    print("-" * 50)
    
    for r in results:
        per_trade = r["net_pnl"] / r["trades"] if r["trades"] > 0 else 0
        print(f"{r['max_opp']}c      {r['trades']:<8} {r['win_rate']:>5.1f}%    ${r['net_pnl']:>7.2f}   ${per_trade:.4f}")


def save_trades(trades: List[FadeTrade], outdir: str = "out_fade_v2"):
    """Save trades to CSV."""
    os.makedirs(outdir, exist_ok=True)
    
    with open(os.path.join(outdir, "trades.csv"), "w", newline="") as f:
        writer = csv.writer(f)
        writer.writerow([
            "window_id", "spike_side", "spike_price", "spike_time",
            "entry_side", "entry_price", "we_win", "final_price",
            "gross_pnl", "fee", "net_pnl"
        ])
        for t in trades:
            writer.writerow([
                t.window_id, t.spike_side, t.spike_price, f"{t.spike_time:.1f}",
                t.entry_side, t.entry_price, t.we_win, t.final_price,
                f"{t.gross_pnl:.4f}", f"{t.fee:.6f}", f"{t.net_pnl:.4f}"
            ])
    
    print(f"\nSaved to {outdir}/")


def main():
    import sys
    
    if len(sys.argv) > 1 and sys.argv[1] == "--sensitivity":
        run_sensitivity()
    else:
        trades = run_fade_backtest(
            spike_threshold=90,
            size_per_trade=10.0,
            max_opp_price=12
        )
        
        analyze_trades(trades, 51)
        save_trades(trades)


if __name__ == "__main__":
    main()

