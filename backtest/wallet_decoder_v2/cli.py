"""
CLI Entry Point for Wallet Decoder V2
"""

from __future__ import annotations

import argparse
import sys
from datetime import datetime
from pathlib import Path

from .config import DecoderV2Config, DEFAULT_TAKER_FEE_BPS, DEFAULT_MAKER_REBATE_BPS
from .data_api import DataAPIClient
from .normalize import normalize_all
from .pairing import run_pairing_engine, compute_pairing_stats
from .classify import compute_maker_taker_stats, classify_strategy, get_best_hypothesis
from .report import generate_all_reports


def parse_args() -> argparse.Namespace:
    """Parse command line arguments."""
    parser = argparse.ArgumentParser(
        description="Wallet Decoder V2 - Infer trading edge from public activity"
    )
    
    parser.add_argument(
        "--user", "-u",
        required=True,
        help="Wallet address"
    )
    
    parser.add_argument(
        "--outdir", "-o",
        default="out_wallet_v2",
        help="Output directory"
    )
    
    parser.add_argument(
        "--start",
        help="Start date filter (ISO format)"
    )
    
    parser.add_argument(
        "--end",
        help="End date filter"
    )
    
    parser.add_argument(
        "--limit",
        type=int,
        default=500,
        help="Page size for API requests"
    )
    
    parser.add_argument(
        "--max-pages",
        type=int,
        default=1000,
        help="Maximum pages to fetch"
    )
    
    parser.add_argument(
        "--taker-fee-bps",
        type=float,
        default=DEFAULT_TAKER_FEE_BPS,
        help=f"Taker fee in basis points (default: {DEFAULT_TAKER_FEE_BPS})"
    )
    
    parser.add_argument(
        "--maker-rebate-bps",
        type=float,
        default=DEFAULT_MAKER_REBATE_BPS,
        help=f"Maker rebate in basis points (default: {DEFAULT_MAKER_REBATE_BPS})"
    )
    
    parser.add_argument(
        "--pair-window",
        type=float,
        default=30.0,
        help="Max seconds between paired buys (default: 30)"
    )
    
    parser.add_argument(
        "--verbose", "-v",
        action="store_true",
        help="Verbose output"
    )
    
    return parser.parse_args()


def parse_date(s: str) -> datetime:
    """Parse date string to datetime."""
    if not s:
        return None
    
    try:
        return datetime.fromisoformat(s)
    except:
        for fmt in ["%Y-%m-%d", "%Y/%m/%d"]:
            try:
                return datetime.strptime(s, fmt)
            except:
                continue
        raise ValueError(f"Could not parse date: {s}")


def main():
    """Main entry point."""
    args = parse_args()
    
    config = DecoderV2Config(
        user_address=args.user,
        outdir=args.outdir,
        start_date=parse_date(args.start) if args.start else None,
        end_date=parse_date(args.end) if args.end else None,
        limit=args.limit,
        max_pages=args.max_pages,
        taker_fee_bps=args.taker_fee_bps,
        maker_rebate_bps=args.maker_rebate_bps,
        pair_window_secs=args.pair_window,
        verbose=args.verbose,
    )
    
    print("=" * 70)
    print("  WALLET DECODER V2")
    print("=" * 70)
    print(f"\nWallet: {config.user_address}")
    print(f"Output: {config.outdir}/")
    print(f"Fee model: Taker {config.taker_fee_bps}bps, Maker rebate {config.maker_rebate_bps}bps")
    print()
    
    # Create output directory
    outdir = Path(config.outdir)
    outdir.mkdir(parents=True, exist_ok=True)
    
    # =========================================================================
    # Step 1: Fetch data
    # =========================================================================
    print("=" * 70)
    print("  STEP 1: Data Collection")
    print("=" * 70)
    
    client = DataAPIClient(config)
    raw_trades, raw_activity = client.fetch_all()
    
    if not raw_trades and not raw_activity:
        print("\nERROR: No data found for this wallet")
        sys.exit(1)
    
    # =========================================================================
    # Step 2: Normalize
    # =========================================================================
    print("\n" + "=" * 70)
    print("  STEP 2: Normalization")
    print("=" * 70)
    
    trades, activity = normalize_all(raw_trades, raw_activity)
    print(f"\nNormalized {len(trades)} trades, {len(activity)} activity events")
    
    # =========================================================================
    # Step 3: Maker/Taker Analysis
    # =========================================================================
    print("\n" + "=" * 70)
    print("  STEP 3: Maker/Taker Analysis")
    print("=" * 70)
    
    maker_taker = compute_maker_taker_stats(trades, config)
    
    print(f"\nMaker/Taker Breakdown:")
    print(f"  Maker: {maker_taker.maker_trades:,} trades ({maker_taker.maker_pct:.1f}%)")
    print(f"  Taker: {maker_taker.taker_trades:,} trades ({maker_taker.taker_pct:.1f}%)")
    print(f"  Unknown: {maker_taker.unknown_trades:,} trades")
    print(f"\n  Est. Fees Paid: ${maker_taker.total_fees_paid:,.2f}")
    print(f"  Est. Rebates Earned: ${maker_taker.total_rebates_earned:,.2f}")
    print(f"  Net Fee Impact: ${maker_taker.net_fee_impact:,.2f}")
    
    # =========================================================================
    # Step 4: Pairing Engine
    # =========================================================================
    print("\n" + "=" * 70)
    print("  STEP 4: Full-Set Pairing Engine")
    print("=" * 70)
    
    windows, pairs = run_pairing_engine(trades, activity, config)
    pair_stats = compute_pairing_stats(pairs)
    
    print(f"\nMarket windows analyzed: {len(windows)}")
    print(f"Full-set pairs detected: {pair_stats['total_pairs']:,}")
    
    if pair_stats['total_pairs'] > 0:
        print(f"\nPair Statistics:")
        print(f"  Total paired size: {pair_stats['total_paired_size']:,.2f} shares")
        print(f"  Avg pair cost: {pair_stats['avg_pair_cost']*100:.2f}c")
        print(f"  Avg pair edge (gross): {pair_stats['avg_pair_edge']*100:.2f}c")
        print(f"  Avg pair edge (net): {pair_stats['avg_net_edge']*100:.2f}c")
        print(f"  Profitable pairs: {pair_stats['profitable_pairs']:,} ({pair_stats['profitable_pct']:.1f}%)")
        print(f"  Total gross edge: ${pair_stats['total_gross_edge']:,.2f}")
        print(f"  Total net edge: ${pair_stats['total_net_edge']:,.2f}")
    
    # =========================================================================
    # Step 5: Strategy Classification
    # =========================================================================
    print("\n" + "=" * 70)
    print("  STEP 5: Strategy Classification")
    print("=" * 70)
    
    hypotheses = classify_strategy(trades, activity, windows, pairs, maker_taker, config)
    best = get_best_hypothesis(hypotheses)
    
    print("\nStrategy Hypotheses (ranked by confidence):")
    for i, hyp in enumerate(hypotheses[:4], 1):
        print(f"\n  {i}. {hyp.name} ({hyp.confidence:.0%})")
        for e in hyp.evidence[:3]:
            print(f"     - {e}")
    
    print(f"\n  ==> BEST HYPOTHESIS: {best.name} ({best.confidence:.0%})")
    
    # =========================================================================
    # Step 6: Key Answer
    # =========================================================================
    print("\n" + "=" * 70)
    print("  KEY QUESTION")
    print("=" * 70)
    
    print("\nDoes this wallet's smooth curve come from full-set arb?")
    
    if pair_stats['total_pairs'] > 0 and pair_stats['avg_pair_edge'] > 0.01:
        print(f"\n  ==> LIKELY YES (via hold-to-settlement)")
        print(f"      {pair_stats['total_pairs']:,} pairs, {pair_stats['avg_pair_edge']*100:.2f}c avg edge")
    elif maker_taker.maker_pct >= 50:
        print(f"\n  ==> PARTIALLY - Market making + incidental full-sets")
        print(f"      {maker_taker.maker_pct:.1f}% maker, ${maker_taker.net_fee_impact:,.2f} net rebates")
    else:
        print(f"\n  ==> UNCLEAR - Need more data")
    
    # =========================================================================
    # Step 7: Generate Reports
    # =========================================================================
    print("\n" + "=" * 70)
    print("  STEP 6: Report Generation")
    print("=" * 70)
    
    generate_all_reports(config, trades, activity, windows, pairs, maker_taker, hypotheses)
    
    print("\n" + "=" * 70)
    print("  COMPLETE")
    print("=" * 70)
    print(f"\nOutput written to: {config.outdir}/")
    print("  - summary.md")
    print("  - maker_taker_stats.csv")
    print("  - fullset_pairs.csv")
    print("  - histograms.json")
    print("  - raw/")


if __name__ == "__main__":
    main()


