"""Corrected daily breakdown - only counting windows with actual trading activity."""
from collections import defaultdict
from fullset_mm_v1.parse import find_window_ids, load_window_ticks
from fullset_mm_v1.stream import merge_tick_streams
from fullset_mm_v1.config import DEFAULT_BUY_DIR, DEFAULT_SELL_DIR


def parse_date_from_window_id(window_id: str) -> str:
    """Extract date from window ID like 25_11_28_14_00_14_15 -> 2025-11-28"""
    parts = window_id.split('_')
    if len(parts) >= 3:
        yy, mm, dd = parts[0], parts[1], parts[2]
        return f"20{yy}-{mm}-{dd}"
    return "unknown"


def main():
    buy_ids = set(find_window_ids(DEFAULT_BUY_DIR))
    sell_ids = set(find_window_ids(DEFAULT_SELL_DIR))
    common = sorted(buy_ids & sell_ids)
    
    print(f"Total windows in dataset: {len(common)}")
    
    # Track by date
    daily_stats = defaultdict(lambda: {
        'total_windows': 0,
        'active_windows': 0,
        'windows_with_opp': 0,
        'total_opps': 0,
        'best_edge': 0,
        'edges': []
    })
    
    active_count = 0
    empty_count = 0
    
    for i, wid in enumerate(common):
        if i % 500 == 0:
            print(f"Processing {i}/{len(common)}...")
        
        date = parse_date_from_window_id(wid)
        daily_stats[date]['total_windows'] += 1
        
        # Load and merge
        buy_ticks, sell_ticks = load_window_ticks(wid, DEFAULT_BUY_DIR, DEFAULT_SELL_DIR)
        
        # Skip if not enough data (inactive window)
        if len(buy_ticks) < 10 or len(sell_ticks) < 10:
            empty_count += 1
            continue
        
        merged = merge_tick_streams(buy_ticks, sell_ticks)
        if len(merged) < 10:
            empty_count += 1
            continue
        
        active_count += 1
        daily_stats[date]['active_windows'] += 1
        
        # Check for opportunities
        window_has_opp = False
        window_opps = 0
        best_edge_this_window = 0
        
        for tick in merged:
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
    
    print(f"\nTotal windows: {len(common)}")
    print(f"Active windows (with trading): {active_count}")
    print(f"Empty/inactive windows: {empty_count}")
    
    # Sort by date
    sorted_dates = sorted(daily_stats.keys())
    
    print(f"\n{'='*100}")
    print("DAILY BREAKDOWN OF FULL-SET OPPORTUNITIES (CORRECTED)")
    print("="*100)
    print(f"{'Date':<12} {'Total':<8} {'Active':<8} {'With Opp':<10} {'Hit Rate':<10} {'Opps':<8} {'Best':<8} {'Avg Edge'}")
    print("-"*100)
    
    total_windows = 0
    total_active = 0
    total_with_opp = 0
    total_opps = 0
    all_edges = []
    
    for date in sorted_dates:
        stats = daily_stats[date]
        hit_rate = stats['windows_with_opp'] / stats['active_windows'] * 100 if stats['active_windows'] > 0 else 0
        avg_edge = sum(stats['edges']) / len(stats['edges']) if stats['edges'] else 0
        
        print(f"{date:<12} {stats['total_windows']:<8} {stats['active_windows']:<8} {stats['windows_with_opp']:<10} {hit_rate:>6.1f}%    {stats['total_opps']:<8} {stats['best_edge']:<8} {avg_edge:.1f}c")
        
        total_windows += stats['total_windows']
        total_active += stats['active_windows']
        total_with_opp += stats['windows_with_opp']
        total_opps += stats['total_opps']
        all_edges.extend(stats['edges'])
    
    print("-"*100)
    overall_hit_rate = total_with_opp / total_active * 100 if total_active > 0 else 0
    overall_avg_edge = sum(all_edges) / len(all_edges) if all_edges else 0
    
    print(f"{'TOTAL':<12} {total_windows:<8} {total_active:<8} {total_with_opp:<10} {overall_hit_rate:>6.1f}%    {total_opps:<8} {max(all_edges) if all_edges else 0:<8} {overall_avg_edge:.1f}c")
    
    print(f"\n{'='*100}")
    print("SUMMARY")
    print("="*100)
    print(f"Total days: {len(sorted_dates)}")
    print(f"Total windows in dataset: {total_windows}")
    print(f"Active trading windows: {total_active} ({total_active/total_windows*100:.1f}%)")
    print(f"Windows with opportunities: {total_with_opp} ({total_with_opp/total_active*100:.1f}% of active)")
    print(f"Average active windows per day: {total_active / len(sorted_dates):.1f}")
    print(f"Average opportunities per day: {total_with_opp / len(sorted_dates):.1f}")
    print(f"Average edge: {overall_avg_edge:.1f}c")
    
    # Edge distribution
    print(f"\n{'='*100}")
    print("EDGE DISTRIBUTION")
    print("="*100)
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
        count = edge_buckets.get(bucket, 0)
        pct = count / len(all_edges) * 100 if all_edges else 0
        print(f"  {bucket:<10}: {count:>5} windows ({pct:>5.1f}%)")


if __name__ == "__main__":
    main()

