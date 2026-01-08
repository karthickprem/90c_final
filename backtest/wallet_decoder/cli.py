"""
CLI Entry Point for Wallet Decoder
"""

from __future__ import annotations

import argparse
import sys
from datetime import datetime
from pathlib import Path

from .config import DecoderConfig
from .data_api import DataAPIClient, apply_date_filter
from .normalize import normalize_all
from .enrich import build_episodes, find_btc_15m_episodes, compute_episode_stats
from .classify import classify_all, compute_label_distribution, get_dominant_strategy
from .pnl import compute_all_pnl, compute_pnl_summary, is_smooth_curve_from_merge
from .report import generate_all_reports


def parse_args() -> argparse.Namespace:
    """Parse command line arguments."""
    parser = argparse.ArgumentParser(
        description="Wallet Decoder - Analyze Polymarket wallet strategies"
    )
    
    parser.add_argument(
        "--user", "-u",
        required=True,
        help="Wallet address (e.g., 0x63ce342161250d705dc0b16df89036c8e5f9ba9a)"
    )
    
    parser.add_argument(
        "--outdir", "-o",
        default="out_wallet",
        help="Output directory (default: out_wallet)"
    )
    
    parser.add_argument(
        "--start",
        help="Start date filter (ISO format, e.g., 2024-01-01)"
    )
    
    parser.add_argument(
        "--end",
        help="End date filter (ISO format)"
    )
    
    parser.add_argument(
        "--limit",
        type=int,
        default=500,
        help="Page size for API requests (default: 500)"
    )
    
    parser.add_argument(
        "--max-pages",
        type=int,
        default=1000,
        help="Maximum pages to fetch (safety limit, default: 1000)"
    )
    
    parser.add_argument(
        "--fee-bps",
        type=float,
        default=0.0,
        help="Fee estimate in basis points (default: 0)"
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
        # Try other formats
        for fmt in ["%Y-%m-%d", "%Y/%m/%d", "%d-%m-%Y"]:
            try:
                return datetime.strptime(s, fmt)
            except:
                continue
        raise ValueError(f"Could not parse date: {s}")


def main():
    """Main entry point."""
    args = parse_args()
    
    # Build config
    config = DecoderConfig(
        user_address=args.user,
        outdir=args.outdir,
        start_date=parse_date(args.start) if args.start else None,
        end_date=parse_date(args.end) if args.end else None,
        limit=args.limit,
        max_pages=args.max_pages,
        fee_bps=args.fee_bps,
        verbose=args.verbose,
    )
    
    print("=" * 70)
    print("  WALLET DECODER")
    print("=" * 70)
    print(f"\nWallet: {config.user_address}")
    print(f"Output: {config.outdir}/")
    if config.start_date:
        print(f"Start:  {config.start_date}")
    if config.end_date:
        print(f"End:    {config.end_date}")
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
    
    # Apply date filter client-side if needed
    if config.start_date or config.end_date:
        raw_trades = apply_date_filter(raw_trades, config.start_date, config.end_date)
        raw_activity = apply_date_filter(raw_activity, config.start_date, config.end_date)
        print(f"After date filter: {len(raw_trades)} trades, {len(raw_activity)} activity")
    
    if not raw_trades and not raw_activity:
        print("\nERROR: No data found for this wallet")
        print("Check if the address is correct and has Polymarket activity")
        sys.exit(1)
    
    # =========================================================================
    # Step 2: Normalize
    # =========================================================================
    print("\n" + "=" * 70)
    print("  STEP 2: Normalization")
    print("=" * 70)
    
    events = normalize_all(raw_trades, raw_activity)
    print(f"\nNormalized {len(events)} events")
    
    # Count by kind
    kinds = {}
    for e in events:
        kinds[e.kind] = kinds.get(e.kind, 0) + 1
    
    print("Event types:")
    for kind, count in sorted(kinds.items(), key=lambda x: -x[1]):
        print(f"  {kind}: {count}")
    
    # =========================================================================
    # Step 3: Build episodes
    # =========================================================================
    print("\n" + "=" * 70)
    print("  STEP 3: Episode Building")
    print("=" * 70)
    
    episodes = build_episodes(events)
    print(f"\nBuilt {len(episodes)} episodes")
    
    # BTC 15m specific
    btc_15m = find_btc_15m_episodes(episodes)
    print(f"BTC 15m episodes: {len(btc_15m)}")
    
    # Stats
    stats = compute_episode_stats(episodes)
    print(f"\nEpisode stats:")
    print(f"  Total trades: {stats['total_trades']}")
    print(f"  MERGE count: {stats['merge_count']}")
    print(f"  REDEEM count: {stats['redeem_count']}")
    print(f"  Full-set candidates: {stats['full_set_candidates']}")
    print(f"  Avg edge: {stats['avg_edge']*100:.2f}c")
    if stats['avg_merge_delay_s']:
        print(f"  Avg merge delay: {stats['avg_merge_delay_s']:.0f}s")
    
    # =========================================================================
    # Step 4: Classification
    # =========================================================================
    print("\n" + "=" * 70)
    print("  STEP 4: Classification")
    print("=" * 70)
    
    classifications = classify_all(episodes)
    label_dist = compute_label_distribution(classifications)
    
    print("\nStrategy distribution:")
    for label, count in sorted(label_dist['counts'].items(), key=lambda x: -x[1]):
        pct = label_dist['percentages'][label]
        print(f"  {label}: {count} ({pct:.1f}%)")
    
    print(f"\nDominant strategy: {get_dominant_strategy(classifications)}")
    
    # =========================================================================
    # Step 5: PnL Reconstruction
    # =========================================================================
    print("\n" + "=" * 70)
    print("  STEP 5: P&L Reconstruction")
    print("=" * 70)
    
    pnl_results = compute_all_pnl(classifications, config.fee_bps)
    pnl_summary = compute_pnl_summary(pnl_results)
    
    print(f"\nP&L Summary:")
    print(f"  Total realized: ${pnl_summary['total_realized']:.2f}")
    print(f"  MERGE P&L: ${pnl_summary['merge_pnl']:.2f} ({pnl_summary['merge_count']} episodes)")
    print(f"  REDEEM P&L: ${pnl_summary['redeem_pnl']:.2f} ({pnl_summary['redeem_count']} episodes)")
    print(f"  Avg edge: {pnl_summary['edge_cents_avg']:.2f}c per matched share")
    
    # =========================================================================
    # Key Question
    # =========================================================================
    print("\n" + "=" * 70)
    print("  KEY QUESTION")
    print("=" * 70)
    
    is_merge_smooth, evidence = is_smooth_curve_from_merge(pnl_summary, label_dist)
    
    print("\nDoes this wallet's smooth curve come mostly from MERGE/full-set arb?")
    print()
    print(evidence)
    
    # =========================================================================
    # Step 6: Generate Reports
    # =========================================================================
    print("\n" + "=" * 70)
    print("  STEP 6: Report Generation")
    print("=" * 70)
    
    generate_all_reports(
        config.outdir,
        config.user_address,
        events,
        episodes,
        classifications,
        pnl_results,
        btc_15m,
    )
    
    print("\n" + "=" * 70)
    print("  COMPLETE")
    print("=" * 70)
    print(f"\nOutput written to: {config.outdir}/")
    print("  - summary.md")
    print("  - episodes.csv")
    print("  - events.jsonl")
    print("  - raw/")
    

if __name__ == "__main__":
    main()


