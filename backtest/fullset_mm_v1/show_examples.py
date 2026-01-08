"""Show sample windows where UP + DOWN < 100c."""
from .stream import load_all_windows
from .config import DEFAULT_BUY_DIR, DEFAULT_SELL_DIR
from .parse import load_window_ticks
from .stream import merge_tick_streams


def find_examples():
    """Find and display windows with full-set opportunities."""
    windows = load_all_windows(DEFAULT_BUY_DIR, DEFAULT_SELL_DIR)
    print(f"Loaded {len(windows)} windows")
    
    # Find windows with full-set opportunities
    examples = []
    for w in windows:
        for tick in w.ticks:
            combined = tick.up_ask + tick.down_ask
            if combined < 100:
                edge = 100 - combined
                examples.append({
                    'window': w.window_id,
                    'time': tick.elapsed_secs,
                    'up_ask': tick.up_ask,
                    'up_bid': tick.up_bid,
                    'down_ask': tick.down_ask,
                    'down_bid': tick.down_bid,
                    'combined': combined,
                    'edge': edge
                })
                break  # Only first opportunity per window
    
    # Sort by edge (best opportunities first)
    examples.sort(key=lambda x: -x['edge'])
    
    print(f"\nFound {len(examples)} windows with UP+DOWN < 100c")
    print(f"\n{'='*80}")
    print("TOP 10 BEST EDGE OPPORTUNITIES")
    print("="*80)
    
    for ex in examples[:10]:
        print(f"\nWindow: {ex['window']}")
        print(f"  Time: {ex['time']:.1f}s into window")
        print(f"  UP:   ask={ex['up_ask']}c, bid={ex['up_bid']}c")
        print(f"  DOWN: ask={ex['down_ask']}c, bid={ex['down_bid']}c")
        print(f"  Combined cost: {ex['combined']}c")
        print(f"  GUARANTEED EDGE: {ex['edge']}c")
    
    return examples


def show_detailed_window(window_id: str):
    """Show detailed tick-by-tick data for a window."""
    buy_ticks, sell_ticks = load_window_ticks(window_id, DEFAULT_BUY_DIR, DEFAULT_SELL_DIR)
    merged = merge_tick_streams(buy_ticks, sell_ticks)
    
    print(f"\n{'='*80}")
    print(f"DETAILED TICK DATA: {window_id}")
    print("="*80)
    print(f"{'Time':<10} {'UP_ask':<10} {'UP_bid':<10} {'DOWN_ask':<10} {'DOWN_bid':<10} {'Combined':<10} {'Edge'}")
    print("-"*80)
    
    shown = 0
    for tick in merged:
        combined = tick.up_ask + tick.down_ask
        edge = 100 - combined if combined < 100 else 0
        
        # Show ticks around the opportunity
        if combined <= 100 or (shown > 0 and shown < 30):
            marker = " <-- OPPORTUNITY!" if combined < 100 else ""
            print(f"{tick.elapsed_secs:<10.1f} {tick.up_ask:<10} {tick.up_bid:<10} {tick.down_ask:<10} {tick.down_bid:<10} {combined:<10} {edge}c{marker}")
            shown += 1
            
            if shown >= 50:
                print("... (truncated)")
                break


def main():
    examples = find_examples()
    
    if examples:
        # Show detailed view of the best example
        best = examples[0]
        show_detailed_window(best['window'])
        
        # Also show a typical 1-2c edge example
        typical = [e for e in examples if e['edge'] <= 2]
        if typical:
            print("\n\n")
            print("="*80)
            print("TYPICAL 1-2c EDGE EXAMPLE (more common)")
            print("="*80)
            show_detailed_window(typical[0]['window'])


if __name__ == "__main__":
    main()

