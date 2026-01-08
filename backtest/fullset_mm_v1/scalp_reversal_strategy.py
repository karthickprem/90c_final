"""
SCALP THE REVERSAL - V3

Key insight:
- 42% of 90c+ spikes have a 10c+ reversal (drop back to 80c)
- BUT only 10% of opposites actually WIN at settlement
- The spike usually RECOVERS after the reversal

Strategy:
- When spike hits 90c, buy the SPIKE side (cheap at 90c)
- Wait for it to run to 95c+ (it usually does)
- OR cut loss if it drops below 85c

This is MOMENTUM trading with reversal risk awareness.
"""
from dataclasses import dataclass
from typing import List, Optional
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
class ScalpTrade:
    """A scalp trade."""
    window_id: str
    
    # Entry
    side: str
    entry_price: int
    entry_time: float
    
    # Exit
    exit_price: int
    exit_time: float
    exit_reason: str  # "target", "stop", "settlement"
    
    # Outcome
    won: bool
    
    # PnL
    gross_pnl: float
    entry_fee: float
    exit_fee: float
    net_pnl: float


def run_scalp_backtest(
    entry_threshold: int = 90,
    target_price: int = 95,
    stop_loss: int = 82,
    size_per_trade: float = 10.0
) -> List[ScalpTrade]:
    """
    Scalp strategy:
    1. Enter when price hits entry_threshold
    2. Exit at target_price (profit) or stop_loss (loss)
    3. Hold to settlement if neither hits
    """
    buy_ids = set(find_window_ids(DEFAULT_BUY_DIR))
    sell_ids = set(find_window_ids(DEFAULT_SELL_DIR))
    common = sorted(buy_ids & sell_ids)
    
    print("=" * 70)
    print("SCALP REVERSAL STRATEGY")
    print("=" * 70)
    print(f"\nConfig:")
    print(f"  Entry: Buy spike side at {entry_threshold}c")
    print(f"  Target: Sell at {target_price}c")
    print(f"  Stop loss: Sell at {stop_loss}c")
    print(f"  Size: ${size_per_trade}")
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
        
        # Track positions
        positions = {}  # side -> entry info
        
        for tick in merged:
            t = tick.elapsed_secs
            
            # Check UP side
            up_price = tick.up_ask
            if "UP" not in positions:
                # Look for entry
                if up_price >= entry_threshold and t < 600:  # Enter in first 10 min
                    positions["UP"] = {
                        "entry_price": up_price,
                        "entry_time": t
                    }
            else:
                # Check for exit
                pos = positions["UP"]
                
                # Use BID for exit (selling)
                sell_price = tick.up_bid
                
                if sell_price >= target_price:
                    # Target hit!
                    trade = create_trade(
                        wid, "UP", pos["entry_price"], pos["entry_time"],
                        sell_price, t, "target", size_per_trade
                    )
                    all_trades.append(trade)
                    del positions["UP"]
                
                elif sell_price <= stop_loss:
                    # Stop hit!
                    trade = create_trade(
                        wid, "UP", pos["entry_price"], pos["entry_time"],
                        sell_price, t, "stop", size_per_trade
                    )
                    all_trades.append(trade)
                    del positions["UP"]
            
            # Check DOWN side
            down_price = tick.down_ask
            if "DOWN" not in positions:
                if down_price >= entry_threshold and t < 600:
                    positions["DOWN"] = {
                        "entry_price": down_price,
                        "entry_time": t
                    }
            else:
                pos = positions["DOWN"]
                sell_price = tick.down_bid
                
                if sell_price >= target_price:
                    trade = create_trade(
                        wid, "DOWN", pos["entry_price"], pos["entry_time"],
                        sell_price, t, "target", size_per_trade
                    )
                    all_trades.append(trade)
                    del positions["DOWN"]
                
                elif sell_price <= stop_loss:
                    trade = create_trade(
                        wid, "DOWN", pos["entry_price"], pos["entry_time"],
                        sell_price, t, "stop", size_per_trade
                    )
                    all_trades.append(trade)
                    del positions["DOWN"]
        
        # Handle positions held to settlement
        for side, pos in positions.items():
            if side == "UP":
                final_price = final.up_ask
            else:
                final_price = final.down_ask
            
            won = final_price >= 97
            exit_price = 100 if won else 0
            
            trade = create_trade(
                wid, side, pos["entry_price"], pos["entry_time"],
                exit_price, 900, "settlement", size_per_trade
            )
            all_trades.append(trade)
    
    return all_trades


def create_trade(
    wid: str, side: str, entry_price: int, entry_time: float,
    exit_price: int, exit_time: float, exit_reason: str, size: float
) -> ScalpTrade:
    """Create a trade with PnL calculation."""
    
    # For settlement at 100 or 0, no exit fee
    if exit_price == 100 or exit_price == 0:
        exit_fee = 0
        if exit_price == 100:
            gross = (100 - entry_price) / 100 * size
        else:
            gross = -entry_price / 100 * size
    else:
        # Intrawindow exit - pay fee on exit too
        gross = (exit_price - entry_price) / 100 * size
        exit_fee = polymarket_fee(exit_price, size)
    
    entry_fee = polymarket_fee(entry_price, size)
    net = gross - entry_fee - exit_fee
    won = gross > 0
    
    return ScalpTrade(
        window_id=wid,
        side=side,
        entry_price=entry_price,
        entry_time=entry_time,
        exit_price=exit_price,
        exit_time=exit_time,
        exit_reason=exit_reason,
        won=won,
        gross_pnl=gross,
        entry_fee=entry_fee,
        exit_fee=exit_fee,
        net_pnl=net
    )


def analyze_trades(trades: List[ScalpTrade], days: int = 51):
    """Analyze results."""
    print("\n" + "=" * 70)
    print("RESULTS")
    print("=" * 70)
    
    if not trades:
        print("No trades!")
        return
    
    n = len(trades)
    wins = sum(1 for t in trades if t.won)
    
    gross = sum(t.gross_pnl for t in trades)
    fees = sum(t.entry_fee + t.exit_fee for t in trades)
    net = sum(t.net_pnl for t in trades)
    
    print(f"\nTotal trades: {n}")
    print(f"Win/Loss: {wins}/{n-wins} ({wins/n*100:.1f}%)")
    
    print(f"\nGross PnL: ${gross:.2f}")
    print(f"Total fees: ${fees:.2f}")
    print(f"NET PnL: ${net:.2f}")
    
    # By exit reason
    by_reason = defaultdict(lambda: {"n": 0, "wins": 0, "gross": 0, "net": 0})
    for t in trades:
        by_reason[t.exit_reason]["n"] += 1
        if t.won:
            by_reason[t.exit_reason]["wins"] += 1
        by_reason[t.exit_reason]["gross"] += t.gross_pnl
        by_reason[t.exit_reason]["net"] += t.net_pnl
    
    print("\n--- By Exit Reason ---")
    print(f"{'Reason':<12} {'N':<8} {'Wins':<8} {'Gross':>10} {'Net':>10}")
    print("-" * 50)
    for reason in ["target", "stop", "settlement"]:
        if reason in by_reason:
            s = by_reason[reason]
            print(f"{reason:<12} {s['n']:<8} {s['wins']:<8} ${s['gross']:>8.2f} ${s['net']:>8.2f}")
    
    # Projection
    print(f"\n--- Projection ---")
    print(f"PnL per day: ${net/days:.2f}")
    print(f"PnL per 30 days: ${net/days*30:.2f}")
    print(f"Annual: ${net/days*365:.2f}")


def run_grid_search():
    """Find optimal entry/target/stop parameters."""
    print("=" * 70)
    print("GRID SEARCH: Optimal Parameters")
    print("=" * 70)
    
    results = []
    
    for entry in [88, 90, 92]:
        for target in [94, 95, 96, 97]:
            for stop in [78, 80, 82, 84]:
                trades = run_scalp_backtest(entry, target, stop, 10.0)
                
                if trades:
                    n = len(trades)
                    wins = sum(1 for t in trades if t.won)
                    net = sum(t.net_pnl for t in trades)
                    
                    results.append({
                        "entry": entry,
                        "target": target,
                        "stop": stop,
                        "trades": n,
                        "win_rate": wins/n*100,
                        "net_pnl": net
                    })
    
    print("\n" + "=" * 70)
    print("TOP 10 CONFIGURATIONS")
    print("=" * 70)
    print(f"{'Entry':>6} {'Target':>7} {'Stop':>6} {'Trades':>8} {'WinRate':>8} {'Net PnL':>10}")
    print("-" * 55)
    
    for r in sorted(results, key=lambda x: x["net_pnl"], reverse=True)[:10]:
        print(f"{r['entry']:>6} {r['target']:>7} {r['stop']:>6} {r['trades']:>8} {r['win_rate']:>7.1f}% ${r['net_pnl']:>9.2f}")


def save_trades(trades: List[ScalpTrade], outdir: str = "out_scalp"):
    """Save to CSV."""
    os.makedirs(outdir, exist_ok=True)
    
    with open(os.path.join(outdir, "trades.csv"), "w", newline="") as f:
        writer = csv.writer(f)
        writer.writerow([
            "window_id", "side", "entry_price", "entry_time",
            "exit_price", "exit_time", "exit_reason", "won",
            "gross_pnl", "entry_fee", "exit_fee", "net_pnl"
        ])
        for t in trades:
            writer.writerow([
                t.window_id, t.side, t.entry_price, f"{t.entry_time:.1f}",
                t.exit_price, f"{t.exit_time:.1f}", t.exit_reason, t.won,
                f"{t.gross_pnl:.4f}", f"{t.entry_fee:.6f}", f"{t.exit_fee:.6f}", f"{t.net_pnl:.4f}"
            ])
    
    print(f"\nSaved to {outdir}/")


def main():
    import sys
    
    if len(sys.argv) > 1 and sys.argv[1] == "--grid":
        run_grid_search()
    else:
        trades = run_scalp_backtest(
            entry_threshold=90,
            target_price=95,
            stop_loss=82,
            size_per_trade=10.0
        )
        
        analyze_trades(trades, 51)
        save_trades(trades)


if __name__ == "__main__":
    main()

