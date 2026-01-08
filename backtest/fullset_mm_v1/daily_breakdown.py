"""Show daily breakdown of full-set opportunities."""
from collections import defaultdict
from .stream import load_all_windows
from .config import DEFAULT_BUY_DIR, DEFAULT_SELL_DIR


def parse_date_from_window_id(window_id: str) -> str:
    """Extract date from window ID like 25_11_28_14_00_14_15 -> 2025-11-28"""
    parts = window_id.split('_')
    if len(parts) >= 3:
        yy, mm, dd = parts[0], parts[1], parts[2]
        return f"20{yy}-{mm}-{dd}"
    return "unknown"


def main():
    windows = load_all_windows(DEFAULT_BUY_DIR, DEFAULT_SELL_DIR)
    print(f"Loaded {len(windows)} windows")
    
    # Track by date
    daily_stats = defaultdict(lambda: {
        'total_windows': 0,
        'windows_with_opp': 0,
        'total_opps': 0,
        'best_edge': 0,
        'edges': []
    })
    
    for w in windows:
        date = parse_date_from_window_id(w.window_id)
        daily_stats[date]['total_windows'] += 1
        
        window_has_opp = False
        window_opps = 0
        best_edge_this_window = 0
        
        for tick in w.ticks:
            combined = tick.up_ask + tick.down_ask
            if combined < 100:
                edge = 100 - combined
                window_opps += 1
                if edge > best_edge_this_window:
                    best_edge_this_window = edge
                window_has_opp = True
        
        if window_has_opp:
            daily_stats[date]['windows_with_opp'] += 1
            daily_stats[date]['total_opps'] += window_opps
            daily_stats[date]['edges'].append(best_edge_this_window)
            if best_edge_this_window > daily_stats[date]['best_edge']:
                daily_stats[date]['best_edge'] = best_edge_this_window
    
    # Sort by date
    sorted_dates = sorted(daily_stats.keys())
    
    print(f"\n{'='*90}")
    print("DAILY BREAKDOWN OF FULL-SET OPPORTUNITIES (UP + DOWN < 100c)")
    print("="*90)
    print(f"{'Date':<12} {'Windows':<10} {'With Opp':<10} {'Hit Rate':<10} {'Total Opps':<12} {'Best Edge':<10} {'Avg Edge'}")
    print("-"*90)
    
    total_windows = 0
    total_with_opp = 0
    total_opps = 0
    all_edges = []
    
    for date in sorted_dates:
        stats = daily_stats[date]
        hit_rate = stats['windows_with_opp'] / stats['total_windows'] * 100 if stats['total_windows'] > 0 else 0
        avg_edge = sum(stats['edges']) / len(stats['edges']) if stats['edges'] else 0
        
        print(f"{date:<12} {stats['total_windows']:<10} {stats['windows_with_opp']:<10} {hit_rate:>6.1f}%    {stats['total_opps']:<12} {stats['best_edge']:<10} {avg_edge:.1f}c")
        
        total_windows += stats['total_windows']
        total_with_opp += stats['windows_with_opp']
        total_opps += stats['total_opps']
        all_edges.extend(stats['edges'])
    
    print("-"*90)
    overall_hit_rate = total_with_opp / total_windows * 100 if total_windows > 0 else 0
    overall_avg_edge = sum(all_edges) / len(all_edges) if all_edges else 0
    
    print(f"{'TOTAL':<12} {total_windows:<10} {total_with_opp:<10} {overall_hit_rate:>6.1f}%    {total_opps:<12} {max(all_edges) if all_edges else 0:<10} {overall_avg_edge:.1f}c")
    
    print(f"\n{'='*90}")
    print("SUMMARY")
    print("="*90)
    print(f"Total days analyzed: {len(sorted_dates)}")
    print(f"Total 15-min windows: {total_windows}")
    print(f"Windows with at least one opportunity: {total_with_opp} ({overall_hit_rate:.1f}%)")
    print(f"Average opportunities per day: {total_with_opp / len(sorted_dates):.1f}")
    print(f"Average edge when opportunity exists: {overall_avg_edge:.1f}c")
    print(f"Best single edge found: {max(all_edges) if all_edges else 0}c")
    
    # Edge distribution
    print(f"\n{'='*90}")
    print("EDGE DISTRIBUTION")
    print("="*90)
    edge_buckets = defaultdict(int)
    for e in all_edges:
        if e >= 10:
            edge_buckets['10c+'] += 1
        elif e >= 5:
            edge_buckets['5-9c'] += 1
        elif e >= 3:
            edge_buckets['3-4c'] += 1
        elif e >= 2:
            edge_buckets['2c'] += 1
        else:
            edge_buckets['1c'] += 1
    
    for bucket in ['1c', '2c', '3-4c', '5-9c', '10c+']:
        count = edge_buckets[bucket]
        pct = count / len(all_edges) * 100 if all_edges else 0
        print(f"  {bucket:<10}: {count:>5} windows ({pct:>5.1f}%)")


if __name__ == "__main__":
    main()

