"""Report generation for full-set MM backtest."""
import json
import csv
import os
from typing import List, Dict
from datetime import datetime

from .sim import SimulationResult
from .metrics import compute_summary_metrics, compute_histogram_distance, normalize_histogram


def ensure_dir(path: str):
    """Ensure directory exists."""
    os.makedirs(path, exist_ok=True)


def write_pairs_csv(result: SimulationResult, outdir: str):
    """Write fullset_pairs.csv with all completed pairs."""
    ensure_dir(outdir)
    filepath = os.path.join(outdir, "fullset_pairs.csv")
    
    fieldnames = [
        'window_id', 'leg1_side', 'leg1_price', 'leg1_time',
        'leg2_side', 'leg2_price', 'leg2_time',
        'dt_between_legs', 'pair_cost', 'edge_cents',
        'completed_via_chase', 'settled_pnl'
    ]
    
    with open(filepath, 'w', newline='', encoding='utf-8') as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        
        for pair in result.all_pairs:
            writer.writerow({
                'window_id': pair.window_id,
                'leg1_side': pair.leg1_side,
                'leg1_price': pair.leg1_price,
                'leg1_time': round(pair.leg1_time, 3),
                'leg2_side': pair.leg2_side,
                'leg2_price': pair.leg2_price,
                'leg2_time': round(pair.leg2_time, 3),
                'dt_between_legs': round(pair.dt_between_legs, 3),
                'pair_cost': pair.pair_cost,
                'edge_cents': pair.edge_cents,
                'completed_via_chase': pair.completed_via_chase,
                'settled_pnl': round(pair.settled_pnl, 4)
            })
    
    print(f"Wrote {len(result.all_pairs)} pairs to {filepath}")


def write_unwinds_csv(result: SimulationResult, outdir: str):
    """Write unwinds.csv with all unwind events."""
    ensure_dir(outdir)
    filepath = os.path.join(outdir, "unwinds.csv")
    
    fieldnames = [
        'window_id', 'side', 'buy_price', 'buy_time',
        'sell_price', 'sell_time', 'pnl_cents'
    ]
    
    with open(filepath, 'w', newline='', encoding='utf-8') as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        
        for unwind in result.all_unwinds:
            writer.writerow({
                'window_id': unwind.window_id,
                'side': unwind.side,
                'buy_price': unwind.buy_price,
                'buy_time': round(unwind.buy_time, 3),
                'sell_price': unwind.sell_price,
                'sell_time': round(unwind.sell_time, 3),
                'pnl_cents': unwind.pnl_cents
            })
    
    print(f"Wrote {len(result.all_unwinds)} unwinds to {filepath}")


def write_histograms_json(result: SimulationResult, outdir: str):
    """Write histograms.json with pair_cost and dt distributions."""
    ensure_dir(outdir)
    filepath = os.path.join(outdir, "histograms.json")
    
    data = {
        'pair_cost_dist': dict(sorted(result.pair_cost_hist.items())),
        'dt_between_legs_dist': dict(sorted(result.dt_hist.items()))
    }
    
    with open(filepath, 'w', encoding='utf-8') as f:
        json.dump(data, f, indent=2)
    
    print(f"Wrote histograms to {filepath}")


def write_summary_json(result: SimulationResult, outdir: str, target_hist: Dict[int, int] = None):
    """Write summary.json with all metrics."""
    ensure_dir(outdir)
    filepath = os.path.join(outdir, "summary.json")
    
    metrics = compute_summary_metrics(result)
    
    # Add histogram match if target provided
    if target_hist:
        dist = compute_histogram_distance(result.pair_cost_hist, target_hist)
        metrics['histogram_match'] = {
            'l1_distance': round(dist.l1_distance, 4),
            'l2_distance': round(dist.l2_distance, 4),
            'overlap_pct': round(dist.overlap_pct, 2)
        }
    
    metrics['timestamp'] = datetime.now().isoformat()
    
    with open(filepath, 'w', encoding='utf-8') as f:
        json.dump(metrics, f, indent=2)
    
    print(f"Wrote summary to {filepath}")


def write_summary_md(result: SimulationResult, outdir: str, target_hist: Dict[int, int] = None):
    """Write summary.md with human-readable report."""
    ensure_dir(outdir)
    filepath = os.path.join(outdir, "summary.md")
    
    metrics = compute_summary_metrics(result)
    
    lines = [
        "# Full-Set MM Backtest Summary",
        "",
        f"**Generated**: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}",
        "",
        "## Strategy Parameters",
        "",
        f"- **d (quote offset)**: {metrics['d_cents']}c",
        f"- **Chase timeout**: {metrics['chase_timeout_secs']}s",
        f"- **Max pair cost**: {metrics['max_pair_cost_cents']}c",
        f"- **Fill model**: {metrics['fill_model']}",
        "",
        "## Activity Summary",
        "",
        f"| Metric | Value |",
        f"|--------|-------|",
        f"| Windows processed | {metrics['windows_processed']:,} |",
        f"| Windows with activity | {metrics['windows_with_activity']:,} |",
        f"| Activity rate | {metrics['activity_rate']*100:.1f}% |",
        "",
        "## Pair Completion",
        "",
        f"| Metric | Value |",
        f"|--------|-------|",
        f"| Total pairs | {metrics['total_pairs']:,} |",
        f"| Profitable pairs | {metrics['profitable_pairs']:,} ({metrics['profit_rate']*100:.1f}%) |",
        f"| Chase-completed | {metrics['chase_completed']:,} ({metrics['chase_rate']*100:.1f}%) |",
        f"| Avg pair cost | {metrics['avg_pair_cost']:.1f}c |",
        f"| Avg edge/pair | {metrics['avg_edge_cents']:.2f}c |",
        f"| Avg dt between legs | {metrics['avg_dt_secs']:.1f}s |",
        "",
        "## Unwind Events",
        "",
        f"| Metric | Value |",
        f"|--------|-------|",
        f"| Total unwinds | {metrics['total_unwinds']:,} |",
        f"| Unwind rate | {metrics['unwind_rate']*100:.1f}% |",
        f"| Avg unwind loss | {metrics['avg_unwind_loss']:.1f}c |",
        "",
        "## PnL Summary",
        "",
        f"| Metric | Value |",
        f"|--------|-------|",
        f"| Gross edge | {metrics['gross_edge_cents']:,}c (${metrics['gross_edge_dollars']:.2f}) |",
        f"| Unwind losses | {metrics['unwind_loss_cents']:,}c |",
        f"| **Net PnL** | **{metrics['net_pnl_cents']:,}c (${metrics['net_pnl_dollars']:.2f})** |",
        "",
        "## Pair Cost Distribution",
        "",
        "| Bucket (c) | Count | Pct |",
        "|------------|-------|-----|",
    ]
    
    # Add histogram rows
    total_pairs = metrics['total_pairs']
    for bucket in sorted(result.pair_cost_hist.keys()):
        count = result.pair_cost_hist[bucket]
        pct = count / max(1, total_pairs) * 100
        lines.append(f"| {bucket} | {count} | {pct:.1f}% |")
    
    # Add histogram match if target provided
    if target_hist:
        dist = compute_histogram_distance(result.pair_cost_hist, target_hist)
        lines.extend([
            "",
            "## Histogram Match (vs @0x8dxd)",
            "",
            f"| Metric | Value |",
            f"|--------|-------|",
            f"| L1 distance | {dist.l1_distance:.4f} |",
            f"| L2 distance | {dist.l2_distance:.4f} |",
            f"| Overlap | {dist.overlap_pct:.1f}% |",
        ])
    
    with open(filepath, 'w', encoding='utf-8') as f:
        f.write('\n'.join(lines))
    
    print(f"Wrote report to {filepath}")


def generate_full_report(
    result: SimulationResult,
    outdir: str,
    target_hist: Dict[int, int] = None
):
    """Generate all report files."""
    write_pairs_csv(result, outdir)
    write_unwinds_csv(result, outdir)
    write_histograms_json(result, outdir)
    write_summary_json(result, outdir, target_hist)
    write_summary_md(result, outdir, target_hist)
    
    print(f"\nAll reports written to {outdir}/")


def write_grid_search_results(
    results: List[SimulationResult],
    outdir: str,
    target_hist: Dict[int, int] = None
):
    """Write grid search results to CSV."""
    ensure_dir(outdir)
    filepath = os.path.join(outdir, "grid_search_results.csv")
    
    fieldnames = [
        'd_cents', 'chase_timeout_secs', 'max_pair_cost_cents', 'fill_model',
        'total_pairs', 'profitable_pairs', 'profit_rate',
        'chase_completed', 'total_unwinds',
        'avg_pair_cost', 'avg_edge_cents',
        'gross_edge_cents', 'unwind_loss_cents', 'net_pnl_cents',
        'l1_distance', 'overlap_pct'
    ]
    
    with open(filepath, 'w', newline='', encoding='utf-8') as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        
        for result in results:
            metrics = compute_summary_metrics(result)
            
            row = {
                'd_cents': metrics['d_cents'],
                'chase_timeout_secs': metrics['chase_timeout_secs'],
                'max_pair_cost_cents': metrics['max_pair_cost_cents'],
                'fill_model': metrics['fill_model'],
                'total_pairs': metrics['total_pairs'],
                'profitable_pairs': metrics['profitable_pairs'],
                'profit_rate': round(metrics['profit_rate'], 4),
                'chase_completed': metrics['chase_completed'],
                'total_unwinds': metrics['total_unwinds'],
                'avg_pair_cost': round(metrics['avg_pair_cost'], 2),
                'avg_edge_cents': round(metrics['avg_edge_cents'], 2),
                'gross_edge_cents': metrics['gross_edge_cents'],
                'unwind_loss_cents': metrics['unwind_loss_cents'],
                'net_pnl_cents': metrics['net_pnl_cents'],
            }
            
            if target_hist:
                dist = compute_histogram_distance(result.pair_cost_hist, target_hist)
                row['l1_distance'] = round(dist.l1_distance, 4)
                row['overlap_pct'] = round(dist.overlap_pct, 2)
            
            writer.writerow(row)
    
    print(f"Wrote grid search results to {filepath}")


