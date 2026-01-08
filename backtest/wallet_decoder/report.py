"""
Report Generation - Create summary.md, episodes.csv, events.jsonl
"""

from __future__ import annotations

import csv
import json
from pathlib import Path
from datetime import datetime
from typing import List, Tuple

from .normalize import Event
from .enrich import Episode
from .classify import Classification, compute_label_distribution
from .pnl import PnLResult, compute_pnl_summary, is_smooth_curve_from_merge


def generate_summary_md(
    outdir: Path,
    user_address: str,
    events: List[Event],
    episodes: List[Episode],
    classifications: List[Tuple[Episode, Classification]],
    pnl_results: List[PnLResult],
    btc_15m_episodes: List[Episode],
) -> str:
    """Generate summary.md report."""
    
    label_dist = compute_label_distribution(classifications)
    pnl_summary = compute_pnl_summary(pnl_results)
    is_merge_smooth, smooth_evidence = is_smooth_curve_from_merge(pnl_summary, label_dist)
    
    # Date range
    if events:
        min_ts = min(e.ts for e in events)
        max_ts = max(e.ts for e in events)
        date_range = f"{min_ts.strftime('%Y-%m-%d')} to {max_ts.strftime('%Y-%m-%d')}"
    else:
        date_range = "N/A"
    
    # Build markdown
    lines = [
        "# Wallet Decoder Report",
        "",
        f"**Wallet:** `{user_address}`",
        f"**Generated:** {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}",
        f"**Date Range:** {date_range}",
        "",
        "---",
        "",
        "## Summary",
        "",
        f"- **Total Events:** {len(events)}",
        f"- **Total Trades:** {len([e for e in events if e.kind == 'TRADE'])}",
        f"- **Total Episodes:** {len(episodes)}",
        f"- **MERGE Events:** {len([e for e in events if e.kind == 'MERGE'])}",
        f"- **REDEEM Events:** {len([e for e in events if e.kind == 'REDEEM'])}",
        f"- **BTC 15m Episodes:** {len(btc_15m_episodes)}",
        "",
        "---",
        "",
        "## Strategy Classification",
        "",
        "| Label | Count | Percentage |",
        "|-------|-------|------------|",
    ]
    
    for label, count in sorted(label_dist['counts'].items(), key=lambda x: -x[1]):
        pct = label_dist['percentages'][label]
        lines.append(f"| {label} | {count} | {pct:.1f}% |")
    
    lines.extend([
        "",
        f"**Dominant Strategy:** {max(label_dist['counts'].items(), key=lambda x: x[1])[0]}",
        "",
        "---",
        "",
        "## P&L Summary",
        "",
        f"- **Total Realized P&L:** ${pnl_summary.get('total_realized', 0):.2f}",
        f"- **Realized Episodes:** {pnl_summary.get('realized_count', 0)}",
        f"- **Unrealized Episodes:** {pnl_summary.get('unrealized_count', 0)}",
        "",
        "### By Resolution Type",
        "",
        f"- **MERGE P&L:** ${pnl_summary.get('merge_pnl', 0):.2f} ({pnl_summary.get('merge_count', 0)} episodes)",
        f"- **REDEEM P&L:** ${pnl_summary.get('redeem_pnl', 0):.2f} ({pnl_summary.get('redeem_count', 0)} episodes)",
        "",
        "### Edge Statistics (MERGE episodes)",
        "",
        f"- **Avg Edge:** {pnl_summary.get('edge_cents_avg', 0):.2f}c per matched share",
        f"- **Min Edge:** {pnl_summary.get('min_edge', 0)*100:.2f}c",
        f"- **Max Edge:** {pnl_summary.get('max_edge', 0)*100:.2f}c",
        "",
        "---",
        "",
        "## Key Question: Does this wallet's smooth curve come mostly from MERGE/full-set arb?",
        "",
        f"**Answer:** {'YES' if is_merge_smooth else 'NO'}",
        "",
        smooth_evidence,
        "",
        "---",
        "",
    ])
    
    # Top episodes by realized profit
    realized_episodes = [
        (ep, cls, pnl) 
        for (ep, cls), pnl in zip(classifications, pnl_results) 
        if pnl.is_realized and pnl.realized_pnl is not None
    ]
    realized_episodes.sort(key=lambda x: x[2].realized_pnl or 0, reverse=True)
    
    if realized_episodes:
        lines.extend([
            "## Top 20 Episodes by Realized Profit",
            "",
            "| Rank | Market | Label | Matched | Edge | P&L |",
            "|------|--------|-------|---------|------|-----|",
        ])
        
        for i, (ep, cls, pnl) in enumerate(realized_episodes[:20], 1):
            market_short = (ep.market_id or "?")[:30]
            edge_cents = pnl.details.get('edge_cents', 0)
            lines.append(
                f"| {i} | {market_short}... | {cls.label} | "
                f"{ep.matched_shares:.1f} | {edge_cents:.2f}c | ${pnl.realized_pnl:.2f} |"
            )
        
        lines.append("")
    
    # BTC 15m specific section
    if btc_15m_episodes:
        lines.extend([
            "---",
            "",
            "## BTC 15m Market Analysis",
            "",
            f"**Total BTC 15m Episodes:** {len(btc_15m_episodes)}",
            "",
        ])
        
        # Find examples of full-set behavior
        full_set_btc = [
            ep for ep in btc_15m_episodes 
            if ep.matched_shares > 0 and ep.has_merge
        ]
        
        if full_set_btc:
            lines.extend([
                "### Examples: Full-Set + MERGE in Same Window",
                "",
            ])
            
            for ep in full_set_btc[:5]:
                lines.extend([
                    f"**Window:** {ep.window_id or ep.market_id}",
                    f"- Bought UP: {ep.total_up_bought:.2f} shares",
                    f"- Bought DOWN: {ep.total_down_bought:.2f} shares",
                    f"- Matched: {ep.matched_shares:.2f} shares",
                    f"- Avg Cost: {ep.avg_cost_matched:.4f} (edge: {(1-ep.avg_cost_matched)*100:.2f}c)",
                    f"- Merge Delay: {ep.merge_delay_s:.0f}s" if ep.merge_delay_s else "- Merge Delay: N/A",
                    "",
                ])
    
    lines.extend([
        "---",
        "",
        "*Generated by wallet_decoder*",
    ])
    
    content = "\n".join(lines)
    
    # Write to file
    summary_file = outdir / "summary.md"
    with open(summary_file, "w") as f:
        f.write(content)
    
    return content


def generate_episodes_csv(
    outdir: Path,
    classifications: List[Tuple[Episode, Classification]],
    pnl_results: List[PnLResult],
) -> None:
    """Generate episodes.csv."""
    
    csv_file = outdir / "episodes.csv"
    
    fieldnames = [
        'episode_id', 'market_id', 'window_id', 'start_ts', 'end_ts',
        'total_trades', 'net_up', 'net_down', 'matched_shares', 'avg_cost_matched',
        'has_merge', 'has_redeem', 'merge_delay_s',
        'label', 'confidence',
        'realized_pnl', 'realized_via', 'is_realized',
    ]
    
    with open(csv_file, 'w', newline='') as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        
        for (ep, cls), pnl in zip(classifications, pnl_results):
            row = {
                'episode_id': pnl.episode_id,
                'market_id': ep.market_id,
                'window_id': ep.window_id,
                'start_ts': ep.start_ts.isoformat() if ep.start_ts else '',
                'end_ts': ep.end_ts.isoformat() if ep.end_ts else '',
                'total_trades': ep.total_trades,
                'net_up': ep.net_up,
                'net_down': ep.net_down,
                'matched_shares': ep.matched_shares,
                'avg_cost_matched': ep.avg_cost_matched,
                'has_merge': ep.has_merge,
                'has_redeem': ep.has_redeem,
                'merge_delay_s': ep.merge_delay_s,
                'label': cls.label,
                'confidence': cls.confidence,
                'realized_pnl': pnl.realized_pnl,
                'realized_via': pnl.realized_via,
                'is_realized': pnl.is_realized,
            }
            writer.writerow(row)


def generate_events_jsonl(outdir: Path, events: List[Event]) -> None:
    """Generate events.jsonl."""
    
    jsonl_file = outdir / "events.jsonl"
    
    with open(jsonl_file, 'w') as f:
        for e in events:
            record = {
                'ts': e.ts.isoformat(),
                'kind': e.kind,
                'market_id': e.market_id,
                'window_id': e.window_id,
                'outcome': e.outcome,
                'side': e.side,
                'price': e.price,
                'size': e.size,
                'cash_delta': e.cash_delta,
                'tx': e.tx,
            }
            f.write(json.dumps(record, default=str) + "\n")


def generate_all_reports(
    outdir: str,
    user_address: str,
    events: List[Event],
    episodes: List[Episode],
    classifications: List[Tuple[Episode, Classification]],
    pnl_results: List[PnLResult],
    btc_15m_episodes: List[Episode],
) -> None:
    """Generate all reports."""
    
    outdir_path = Path(outdir)
    outdir_path.mkdir(parents=True, exist_ok=True)
    
    print("\nGenerating reports...")
    
    # summary.md
    generate_summary_md(
        outdir_path, user_address, events, episodes,
        classifications, pnl_results, btc_15m_episodes
    )
    print(f"  Written: {outdir_path / 'summary.md'}")
    
    # episodes.csv
    generate_episodes_csv(outdir_path, classifications, pnl_results)
    print(f"  Written: {outdir_path / 'episodes.csv'}")
    
    # events.jsonl
    generate_events_jsonl(outdir_path, events)
    print(f"  Written: {outdir_path / 'events.jsonl'}")


