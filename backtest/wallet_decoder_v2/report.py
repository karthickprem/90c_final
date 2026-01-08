"""
Report Generation V2 - Enhanced outputs with maker/taker and pairing stats
"""

from __future__ import annotations

import csv
import json
from pathlib import Path
from datetime import datetime
from typing import List, Dict

from .normalize import TradeEvent, ActivityEvent
from .pairing import MarketWindow, FullSetPair, compute_pairing_stats
from .classify import MakerTakerStats, StrategyHypothesis, get_best_hypothesis
from .config import DecoderV2Config


def generate_summary_md(
    outdir: Path,
    config: DecoderV2Config,
    trades: List[TradeEvent],
    activity: List[ActivityEvent],
    windows: Dict[str, MarketWindow],
    pairs: List[FullSetPair],
    maker_taker: MakerTakerStats,
    hypotheses: List[StrategyHypothesis],
) -> str:
    """Generate comprehensive summary.md report."""
    
    pair_stats = compute_pairing_stats(pairs)
    best = get_best_hypothesis(hypotheses)
    
    # Date range
    if trades:
        min_ts = min(t.ts for t in trades)
        max_ts = max(t.ts for t in trades)
        date_range = f"{min_ts.strftime('%Y-%m-%d')} to {max_ts.strftime('%Y-%m-%d')}"
    else:
        date_range = "N/A"
    
    # Activity counts
    redeem_count = len([a for a in activity if a.kind == "REDEEM"])
    merge_count = len([a for a in activity if a.kind == "MERGE"])
    
    lines = [
        "# Wallet Decoder V2 Report",
        "",
        f"**Wallet:** `{config.user_address}`",
        f"**Generated:** {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}",
        f"**Date Range:** {date_range}",
        "",
        "---",
        "",
        "## Summary",
        "",
        f"- **Total Trades:** {len(trades):,}",
        f"- **Total Markets:** {len(windows):,}",
        f"- **MERGE Events:** {merge_count:,}",
        f"- **REDEEM Events:** {redeem_count:,}",
        "",
        "---",
        "",
        "## Maker vs Taker Analysis",
        "",
        "| Metric | Value |",
        "|--------|-------|",
        f"| Total Trades | {maker_taker.total_trades:,} |",
        f"| **Maker Trades** | {maker_taker.maker_trades:,} ({maker_taker.maker_pct:.1f}%) |",
        f"| **Taker Trades** | {maker_taker.taker_trades:,} ({maker_taker.taker_pct:.1f}%) |",
        f"| Unknown | {maker_taker.unknown_trades:,} |",
        f"| Maker Volume | ${maker_taker.maker_volume:,.2f} |",
        f"| Taker Volume | ${maker_taker.taker_volume:,.2f} |",
        f"| **Est. Fees Paid** | ${maker_taker.total_fees_paid:,.2f} |",
        f"| **Est. Rebates Earned** | ${maker_taker.total_rebates_earned:,.2f} |",
        f"| **Net Fee Impact** | ${maker_taker.net_fee_impact:,.2f} |",
        "",
        "---",
        "",
        "## Full-Set Pair Analysis",
        "",
        f"**Key Insight:** Full-set arb does NOT require MERGE. You can buy YES + NO, hold to settlement, and redeem the winner for $1.",
        "",
        "| Metric | Value |",
        "|--------|-------|",
        f"| **Pairs Detected** | {pair_stats['total_pairs']:,} |",
        f"| Total Paired Size | {pair_stats['total_paired_size']:,.2f} shares |",
        f"| Avg Pair Cost | {pair_stats['avg_pair_cost']*100:.2f}c |",
        f"| **Avg Pair Edge (gross)** | {pair_stats['avg_pair_edge']*100:.2f}c |",
        f"| **Avg Pair Edge (net of fees)** | {pair_stats['avg_net_edge']*100:.2f}c |",
        f"| Profitable Pairs | {pair_stats['profitable_pairs']:,} ({pair_stats['profitable_pct']:.1f}%) |",
        f"| **Total Gross Edge** | ${pair_stats['total_gross_edge']:,.2f} |",
        f"| **Total Net Edge** | ${pair_stats['total_net_edge']:,.2f} |",
        f"| Avg Delay Between Legs | {pair_stats['avg_delay_secs']:.1f}s |",
        f"| Pairs Held to Settlement | {pair_stats['pairs_with_redeem']:,} |",
    ]
    
    if pair_stats['avg_hold_time_secs']:
        lines.append(f"| Avg Hold Time | {pair_stats['avg_hold_time_secs']/60:.1f} min |")
    
    lines.extend([
        "",
        "---",
        "",
        "## Strategy Hypotheses (Ranked by Confidence)",
        "",
    ])
    
    for i, hyp in enumerate(hypotheses[:5], 1):
        lines.extend([
            f"### {i}. {hyp.name} (Confidence: {hyp.confidence:.0%})",
            "",
            f"*{hyp.description}*",
            "",
            "**Evidence:**",
        ])
        for e in hyp.evidence:
            lines.append(f"- {e}")
        lines.append("")
    
    # Best hypothesis section
    lines.extend([
        "---",
        "",
        "## BEST HYPOTHESIS",
        "",
        f"### {best.name}",
        "",
        f"**Confidence:** {best.confidence:.0%}",
        "",
        f"*{best.description}*",
        "",
        "**Quantified Evidence:**",
    ])
    
    for e in best.evidence:
        lines.append(f"- {e}")
    
    if best.metrics:
        lines.extend(["", "**Key Metrics:**"])
        for k, v in best.metrics.items():
            if isinstance(v, float):
                lines.append(f"- {k}: {v:,.2f}")
            else:
                lines.append(f"- {k}: {v:,}" if isinstance(v, int) else f"- {k}: {v}")
    
    # Answer the key question
    lines.extend([
        "",
        "---",
        "",
        "## Key Question: Does this wallet's smooth curve come from full-set arb?",
        "",
    ])
    
    # Determine answer based on analysis
    is_fullset = any(h.name in ["FULL_SET_HOLD_TO_SETTLEMENT", "REBATE_MARKET_MAKER"] and h.confidence >= 0.4 for h in hypotheses)
    
    if pair_stats['total_pairs'] > 0 and pair_stats['avg_pair_edge'] > 0.01:
        lines.extend([
            "**Answer: LIKELY YES (via hold-to-settlement, not MERGE)**",
            "",
            "Evidence:",
            f"- {pair_stats['total_pairs']:,} full-set pairs detected",
            f"- Avg edge: {pair_stats['avg_pair_edge']*100:.2f}c per pair",
            f"- Total edge captured: ${pair_stats['total_gross_edge']:,.2f}",
        ])
    elif maker_taker.maker_pct >= 50:
        lines.extend([
            "**Answer: PARTIALLY - Primarily MARKET MAKING with incidental full-sets**",
            "",
            "Evidence:",
            f"- {maker_taker.maker_pct:.1f}% maker trades (earning rebates)",
            f"- Net rebate earnings: ${maker_taker.net_fee_impact:,.2f}",
            f"- When both sides fill, that becomes full-set inventory",
        ])
    else:
        lines.extend([
            "**Answer: UNCLEAR - Insufficient full-set pattern detected**",
            "",
            "Evidence:",
            f"- Only {pair_stats['total_pairs']} pairs detected",
            f"- {maker_taker.maker_pct:.1f}% maker ratio",
        ])
    
    lines.extend([
        "",
        "---",
        "",
        f"*Generated by wallet_decoder_v2*",
    ])
    
    content = "\n".join(lines)
    
    summary_file = outdir / "summary.md"
    with open(summary_file, "w") as f:
        f.write(content)
    
    return content


def generate_maker_taker_csv(
    outdir: Path,
    trades: List[TradeEvent],
) -> None:
    """Generate maker_taker_stats.csv with per-trade liquidity."""
    csv_file = outdir / "maker_taker_stats.csv"
    
    fieldnames = [
        'trade_id', 'ts', 'market_id', 'outcome', 'side',
        'price', 'size', 'notional', 'liquidity',
    ]
    
    with open(csv_file, 'w', newline='') as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        
        for t in trades:
            writer.writerow({
                'trade_id': t.trade_id,
                'ts': t.ts.isoformat(),
                'market_id': t.market_id[:30] if t.market_id else "",
                'outcome': t.outcome,
                'side': t.side,
                'price': t.price,
                'size': t.size,
                'notional': t.notional,
                'liquidity': t.liquidity,
            })


def generate_fullset_pairs_csv(
    outdir: Path,
    pairs: List[FullSetPair],
) -> None:
    """Generate fullset_pairs.csv with all detected pairs."""
    csv_file = outdir / "fullset_pairs.csv"
    
    fieldnames = [
        'market_id', 'yes_ts', 'yes_price', 'yes_size', 'yes_liquidity',
        'no_ts', 'no_price', 'no_size', 'no_liquidity',
        'pair_size', 'pair_cost', 'pair_edge', 'net_edge',
        'delay_secs', 'has_redeem', 'hold_time_secs',
    ]
    
    with open(csv_file, 'w', newline='') as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        
        for p in pairs:
            writer.writerow({
                'market_id': p.market_id[:30] if p.market_id else "",
                'yes_ts': p.yes_ts.isoformat(),
                'yes_price': p.yes_price,
                'yes_size': p.yes_size,
                'yes_liquidity': p.yes_trade.liquidity,
                'no_ts': p.no_ts.isoformat(),
                'no_price': p.no_price,
                'no_size': p.no_size,
                'no_liquidity': p.no_trade.liquidity,
                'pair_size': p.pair_size,
                'pair_cost': p.pair_cost,
                'pair_edge': p.pair_edge,
                'net_edge': p.net_edge,
                'delay_secs': p.pair_delay_secs,
                'has_redeem': p.has_redeem,
                'hold_time_secs': p.hold_time_secs,
            })


def generate_histograms_json(
    outdir: Path,
    trades: List[TradeEvent],
    pairs: List[FullSetPair],
) -> None:
    """Generate histogram data for visualization."""
    
    # Entry price distribution
    entry_prices = [t.price for t in trades if t.side == "BUY"]
    price_buckets = {}
    for p in entry_prices:
        bucket = int(p * 100) // 5 * 5  # 5c buckets
        price_buckets[bucket] = price_buckets.get(bucket, 0) + 1
    
    # Pair cost distribution
    pair_costs = [p.pair_cost for p in pairs]
    cost_buckets = {}
    for c in pair_costs:
        bucket = int(c * 100) // 2 * 2  # 2c buckets
        cost_buckets[bucket] = cost_buckets.get(bucket, 0) + 1
    
    # Pair delay distribution
    delay_buckets = {}
    for p in pairs:
        bucket = int(p.pair_delay_secs) // 5 * 5  # 5s buckets
        delay_buckets[bucket] = delay_buckets.get(bucket, 0) + 1
    
    histograms = {
        'entry_price_dist': dict(sorted(price_buckets.items())),
        'pair_cost_dist': dict(sorted(cost_buckets.items())),
        'pair_delay_dist': dict(sorted(delay_buckets.items())),
    }
    
    with open(outdir / "histograms.json", "w") as f:
        json.dump(histograms, f, indent=2)


def generate_all_reports(
    config: DecoderV2Config,
    trades: List[TradeEvent],
    activity: List[ActivityEvent],
    windows: Dict[str, MarketWindow],
    pairs: List[FullSetPair],
    maker_taker: MakerTakerStats,
    hypotheses: List[StrategyHypothesis],
) -> None:
    """Generate all V2 reports."""
    
    outdir = Path(config.outdir)
    outdir.mkdir(parents=True, exist_ok=True)
    
    print("\nGenerating V2 reports...")
    
    generate_summary_md(outdir, config, trades, activity, windows, pairs, maker_taker, hypotheses)
    print(f"  Written: {outdir / 'summary.md'}")
    
    generate_maker_taker_csv(outdir, trades)
    print(f"  Written: {outdir / 'maker_taker_stats.csv'}")
    
    generate_fullset_pairs_csv(outdir, pairs)
    print(f"  Written: {outdir / 'fullset_pairs.csv'}")
    
    generate_histograms_json(outdir, trades, pairs)
    print(f"  Written: {outdir / 'histograms.json'}")


