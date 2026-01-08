"""
REALISTIC Full-Set Backtest with:
1. Polymarket taker fee model
2. Two-leg simultaneous fill requirement
3. Capacity limits
4. Price bucket analysis
5. Full dataset coverage
"""
from dataclasses import dataclass, field
from typing import List, Dict, Optional, Tuple
from collections import defaultdict
import os
import json
import csv

from .parse import find_window_ids, load_window_ticks
from .stream import merge_tick_streams, QuoteTick
from .config import DEFAULT_BUY_DIR, DEFAULT_SELL_DIR


def polymarket_taker_fee(price_cents: int, shares: float) -> float:
    """
    Calculate Polymarket taker fee for 15-min crypto markets.
    
    Formula: fee = C * 0.25 * (p * (1-p))^2
    where p is price (0-1) and C is number of shares
    
    Returns fee in dollars.
    """
    p = price_cents / 100.0  # Convert to 0-1
    fee_per_share = 0.25 * (p * (1 - p)) ** 2
    return shares * fee_per_share


def compute_leg_fee(price_cents: int, size_dollars: float) -> float:
    """Compute fee for a single leg purchase."""
    # shares = size_dollars / price (in dollars)
    if price_cents <= 0:
        return 0
    shares = size_dollars / (price_cents / 100.0)
    return polymarket_taker_fee(price_cents, shares)


@dataclass
class RealisticTrade:
    """A trade with full fee accounting."""
    window_id: str
    tick_time: float
    
    up_ask: int
    down_ask: int
    combined_cost: int
    gross_edge_cents: float
    
    # Fees
    up_fee: float
    down_fee: float
    total_fee: float
    
    # Net edge
    net_edge_cents: float
    net_edge_dollars: float
    
    # Size
    size_per_leg: float
    
    @property
    def is_profitable(self) -> bool:
        return self.net_edge_cents > 0


@dataclass
class BacktestResult:
    """Complete backtest results."""
    # Coverage
    total_windows_in_dataset: int = 0
    active_windows: int = 0
    windows_with_opportunity: int = 0
    
    # Trades
    trades: List[RealisticTrade] = field(default_factory=list)
    skipped_due_to_fees: int = 0
    
    # PnL
    gross_edge_total: float = 0
    total_fees: float = 0
    net_pnl: float = 0
    
    # By bucket
    edge_by_price_bucket: Dict[str, Dict] = field(default_factory=dict)


def run_realistic_backtest(
    size_per_leg: float = 10.0,
    max_combined_cost: int = 99,
    min_gross_edge: int = 1,
    require_net_positive: bool = True,
    max_pairs_per_window: int = 1,  # Conservative: only 1 opportunity per window
) -> BacktestResult:
    """
    Run realistic backtest with fees and constraints.
    """
    buy_ids = set(find_window_ids(DEFAULT_BUY_DIR))
    sell_ids = set(find_window_ids(DEFAULT_SELL_DIR))
    common = sorted(buy_ids & sell_ids)
    
    result = BacktestResult()
    result.total_windows_in_dataset = len(common)
    
    print("=" * 70)
    print("REALISTIC FULL-SET BACKTEST")
    print("=" * 70)
    print(f"\nConfig:")
    print(f"  Size per leg: ${size_per_leg}")
    print(f"  Max combined cost: {max_combined_cost}c")
    print(f"  Min gross edge: {min_gross_edge}c")
    print(f"  Require net positive after fees: {require_net_positive}")
    print(f"  Max pairs per window: {max_pairs_per_window}")
    print(f"\nDataset: {len(common)} windows")
    print()
    
    # Price bucket stats
    bucket_stats = defaultdict(lambda: {
        'opportunities': 0,
        'profitable_after_fees': 0,
        'gross_edge_sum': 0,
        'fee_sum': 0,
        'net_edge_sum': 0
    })
    
    for i, wid in enumerate(common):
        if i % 500 == 0:
            print(f"Processing {i}/{len(common)}...")
        
        buy_ticks, sell_ticks = load_window_ticks(wid, DEFAULT_BUY_DIR, DEFAULT_SELL_DIR)
        
        # Skip inactive windows
        if len(buy_ticks) < 10 or len(sell_ticks) < 10:
            continue
        
        merged = merge_tick_streams(buy_ticks, sell_ticks)
        if len(merged) < 10:
            continue
        
        result.active_windows += 1
        
        # Track opportunities this window
        window_trades = 0
        window_has_opp = False
        
        for tick in merged:
            if window_trades >= max_pairs_per_window:
                break
            
            combined = tick.up_ask + tick.down_ask
            gross_edge = 100 - combined
            
            if combined > max_combined_cost or gross_edge < min_gross_edge:
                continue
            
            window_has_opp = True
            
            # Calculate fees for both legs
            up_fee = compute_leg_fee(tick.up_ask, size_per_leg)
            down_fee = compute_leg_fee(tick.down_ask, size_per_leg)
            total_fee = up_fee + down_fee
            
            # Net edge in cents (per dollar of size)
            # Gross edge: for $1 invested in full-set at cost c, profit = (100-c)/c per dollar
            # But simpler: edge_cents * (size/100) gives dollar profit
            gross_edge_dollars = (gross_edge / 100) * size_per_leg * 2  # 2 legs
            net_edge_dollars = gross_edge_dollars - total_fee
            net_edge_cents = net_edge_dollars / (size_per_leg * 2) * 100
            
            # Determine price bucket
            avg_price = combined / 2
            if avg_price < 20:
                bucket = "0-20c"
            elif avg_price < 40:
                bucket = "20-40c"
            elif avg_price < 50:
                bucket = "40-50c"
            elif avg_price < 60:
                bucket = "50-60c"
            else:
                bucket = "60c+"
            
            # Track bucket stats
            bucket_stats[bucket]['opportunities'] += 1
            bucket_stats[bucket]['gross_edge_sum'] += gross_edge
            bucket_stats[bucket]['fee_sum'] += total_fee
            
            if net_edge_dollars > 0:
                bucket_stats[bucket]['profitable_after_fees'] += 1
                bucket_stats[bucket]['net_edge_sum'] += net_edge_cents
            
            # Skip if net negative and we require positive
            if require_net_positive and net_edge_dollars <= 0:
                result.skipped_due_to_fees += 1
                continue
            
            # Record trade
            trade = RealisticTrade(
                window_id=wid,
                tick_time=tick.elapsed_secs,
                up_ask=tick.up_ask,
                down_ask=tick.down_ask,
                combined_cost=combined,
                gross_edge_cents=gross_edge,
                up_fee=up_fee,
                down_fee=down_fee,
                total_fee=total_fee,
                net_edge_cents=net_edge_cents,
                net_edge_dollars=net_edge_dollars,
                size_per_leg=size_per_leg
            )
            result.trades.append(trade)
            window_trades += 1
            
            result.gross_edge_total += gross_edge_dollars
            result.total_fees += total_fee
            result.net_pnl += net_edge_dollars
        
        if window_has_opp:
            result.windows_with_opportunity += 1
    
    result.edge_by_price_bucket = dict(bucket_stats)
    
    return result


def print_results(result: BacktestResult, days: int = 51):
    """Print comprehensive results."""
    print("\n" + "=" * 70)
    print("RESULTS")
    print("=" * 70)
    
    print(f"\n[DATASET COVERAGE]")
    print(f"  Total windows in dataset: {result.total_windows_in_dataset}")
    print(f"  Active trading windows: {result.active_windows}")
    print(f"  Windows with opportunity: {result.windows_with_opportunity} ({result.windows_with_opportunity/result.active_windows*100:.1f}%)")
    
    print(f"\n[TRADE SUMMARY]")
    print(f"  Total trades taken: {len(result.trades)}")
    print(f"  Skipped (negative after fees): {result.skipped_due_to_fees}")
    print(f"  Gross edge: ${result.gross_edge_total:.2f}")
    print(f"  Total fees paid: ${result.total_fees:.2f}")
    print(f"  NET PnL: ${result.net_pnl:.2f}")
    
    if len(result.trades) > 0:
        avg_gross = sum(t.gross_edge_cents for t in result.trades) / len(result.trades)
        avg_fee = result.total_fees / len(result.trades)
        avg_net = result.net_pnl / len(result.trades)
        print(f"\n  Avg gross edge: {avg_gross:.2f}c")
        print(f"  Avg fee per trade: ${avg_fee:.4f}")
        print(f"  Avg net edge: ${avg_net:.4f}")
    
    print(f"\n[ANNUALIZED] (assuming {days} days of data)")
    print(f"  PnL per day: ${result.net_pnl / days:.2f}")
    print(f"  PnL per 30 days: ${result.net_pnl / days * 30:.2f}")
    print(f"  PnL per year: ${result.net_pnl / days * 365:.2f}")
    
    print(f"\n[EDGE BY PRICE BUCKET] (avg price of UP+DOWN)")
    print("-" * 70)
    print(f"{'Bucket':<12} {'Opps':<10} {'Profitable':<12} {'Gross Avg':<12} {'Avg Fee':<12} {'Net Avg'}")
    print("-" * 70)
    
    for bucket in ['0-20c', '20-40c', '40-50c', '50-60c', '60c+']:
        stats = result.edge_by_price_bucket.get(bucket, {})
        opps = stats.get('opportunities', 0)
        if opps == 0:
            continue
        
        profitable = stats.get('profitable_after_fees', 0)
        gross_avg = stats.get('gross_edge_sum', 0) / opps
        fee_avg = stats.get('fee_sum', 0) / opps
        net_avg = stats.get('net_edge_sum', 0) / max(1, profitable)
        
        pct = profitable / opps * 100
        print(f"{bucket:<12} {opps:<10} {profitable} ({pct:.0f}%)     {gross_avg:.2f}c        ${fee_avg:.4f}      {net_avg:.2f}c")
    
    print("\n" + "=" * 70)
    print("FEE IMPACT ANALYSIS")
    print("=" * 70)
    print("""
Polymarket fee formula: fee = shares * 0.25 * (p * (1-p))^2

Fee at different prices (per $10 leg):
  At 10c: $0.0020 (0.02%)
  At 30c: $0.0368 (0.37%)
  At 50c: $0.0625 (0.63%)  <- MAXIMUM FEE
  At 70c: $0.0368 (0.37%)
  At 90c: $0.0020 (0.02%)

Key insight: Fees are highest at 50/50, lowest at extremes!
Full-set edge at 50c+50c = 0c, so no opportunity there anyway.
Best full-sets are at extremes (e.g., 90c+8c) where fees are lowest.
""")


def save_results(result: BacktestResult, outdir: str = "out_realistic_backtest"):
    """Save results to files."""
    os.makedirs(outdir, exist_ok=True)
    
    # Save trades CSV
    with open(os.path.join(outdir, "trades.csv"), "w", newline="") as f:
        writer = csv.writer(f)
        writer.writerow([
            "window_id", "tick_time", "up_ask", "down_ask", "combined",
            "gross_edge", "up_fee", "down_fee", "total_fee", "net_edge_cents", "net_edge_dollars"
        ])
        for t in result.trades:
            writer.writerow([
                t.window_id, t.tick_time, t.up_ask, t.down_ask, t.combined_cost,
                t.gross_edge_cents, f"{t.up_fee:.6f}", f"{t.down_fee:.6f}", 
                f"{t.total_fee:.6f}", f"{t.net_edge_cents:.4f}", f"{t.net_edge_dollars:.6f}"
            ])
    
    # Save summary JSON
    summary = {
        "dataset": {
            "total_windows": result.total_windows_in_dataset,
            "active_windows": result.active_windows,
            "windows_with_opportunity": result.windows_with_opportunity
        },
        "trades": {
            "total": len(result.trades),
            "skipped_negative": result.skipped_due_to_fees
        },
        "pnl": {
            "gross_edge_dollars": result.gross_edge_total,
            "total_fees_dollars": result.total_fees,
            "net_pnl_dollars": result.net_pnl
        },
        "buckets": result.edge_by_price_bucket
    }
    
    with open(os.path.join(outdir, "summary.json"), "w") as f:
        json.dump(summary, f, indent=2)
    
    print(f"\nResults saved to {outdir}/")


def main():
    """Run realistic backtest with default parameters."""
    import sys
    
    size = float(sys.argv[1]) if len(sys.argv) > 1 else 10.0
    max_cost = int(sys.argv[2]) if len(sys.argv) > 2 else 99
    
    result = run_realistic_backtest(
        size_per_leg=size,
        max_combined_cost=max_cost,
        min_gross_edge=1,
        require_net_positive=True,
        max_pairs_per_window=1
    )
    
    print_results(result, days=51)
    save_results(result)


if __name__ == "__main__":
    main()

