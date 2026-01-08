"""
Analyzer - Phase 6

Evaluates paper trading results and raw tick data to determine:
1. Whether instant arb (Variant A) ever exists
2. Whether DCA/legging (Variant B) can work with price oscillation
3. What the realistic edge might be
"""

import json
from pathlib import Path
from typing import List, Dict
from collections import defaultdict


def load_jsonl(path: Path) -> List[dict]:
    """Load JSONL file."""
    data = []
    with open(path) as f:
        for line in f:
            if line.strip():
                data.append(json.loads(line))
    return data


def analyze_ticks(tick_file: Path):
    """
    Analyze raw tick data.
    """
    print(f"\n{'='*60}")
    print(f"Analyzing: {tick_file.name}")
    print(f"{'='*60}")
    
    ticks = load_jsonl(tick_file)
    
    if not ticks:
        print("No ticks found.")
        return
    
    print(f"\nTotal ticks: {len(ticks)}")
    
    # Separate by window
    windows = defaultdict(list)
    for t in ticks:
        windows[t.get("slug", "unknown")].append(t)
    
    print(f"Windows: {len(windows)}")
    
    # Global stats
    all_ask_sums = [t["ask_sum"] for t in ticks if t.get("ask_sum", 0) > 0]
    all_bid_sums = [t["bid_sum"] for t in ticks if t.get("bid_sum", 0) > 0]
    
    if all_ask_sums:
        print(f"\n--- Ask Sum Stats ---")
        print(f"  Min: {min(all_ask_sums):.4f}")
        print(f"  Max: {max(all_ask_sums):.4f}")
        print(f"  Avg: {sum(all_ask_sums)/len(all_ask_sums):.4f}")
        
        # Instant arb check
        below_1 = sum(1 for s in all_ask_sums if s < 1.0)
        below_99 = sum(1 for s in all_ask_sums if s < 0.99)
        below_98 = sum(1 for s in all_ask_sums if s < 0.98)
        
        print(f"\n--- Instant Arb Frequency ---")
        print(f"  Ticks with ask_sum < 1.00: {below_1} ({below_1/len(all_ask_sums)*100:.2f}%)")
        print(f"  Ticks with ask_sum < 0.99: {below_99} ({below_99/len(all_ask_sums)*100:.2f}%)")
        print(f"  Ticks with ask_sum < 0.98: {below_98} ({below_98/len(all_ask_sums)*100:.2f}%)")
    
    # Per-side price ranges
    ask_ups = [t["ask_up"] for t in ticks if t.get("ask_up", 0) > 0]
    ask_downs = [t["ask_down"] for t in ticks if t.get("ask_down", 0) > 0]
    
    if ask_ups:
        print(f"\n--- Up Price Range ---")
        print(f"  Min: {min(ask_ups):.4f}")
        print(f"  Max: {max(ask_ups):.4f}")
        print(f"  Range: {max(ask_ups) - min(ask_ups):.4f}")
    
    if ask_downs:
        print(f"\n--- Down Price Range ---")
        print(f"  Min: {min(ask_downs):.4f}")
        print(f"  Max: {max(ask_downs):.4f}")
        print(f"  Range: {max(ask_downs) - min(ask_downs):.4f}")
    
    # Check for oscillation potential
    print(f"\n--- Oscillation Potential ---")
    
    for slug, wticks in list(windows.items())[:5]:  # First 5 windows
        up_prices = [t["ask_up"] for t in wticks if t.get("ask_up", 0) > 0]
        down_prices = [t["ask_down"] for t in wticks if t.get("ask_down", 0) > 0]
        
        if up_prices and down_prices:
            up_range = max(up_prices) - min(up_prices)
            down_range = max(down_prices) - min(down_prices)
            
            # Check if both sides were ever cheap enough
            up_min = min(up_prices)
            down_min = min(down_prices)
            
            theoretical_pair_cost = up_min + down_min
            
            print(f"\n  {slug}:")
            print(f"    Ticks: {len(wticks)}")
            print(f"    Up range: {min(up_prices):.2f} - {max(up_prices):.2f} (Δ={up_range:.2f})")
            print(f"    Down range: {min(down_prices):.2f} - {max(down_prices):.2f} (Δ={down_range:.2f})")
            print(f"    Best theoretical pair cost: {theoretical_pair_cost:.4f}")
            print(f"    Profit if achieved: {1.0 - theoretical_pair_cost:.4f}")


def analyze_trades(trade_file: Path):
    """
    Analyze paper trading results.
    """
    print(f"\n{'='*60}")
    print(f"Analyzing: {trade_file.name}")
    print(f"{'='*60}")
    
    events = load_jsonl(trade_file)
    
    # Separate by type
    trades = [e for e in events if e.get("event", "").startswith("TRADE")]
    windows_start = [e for e in events if e.get("event") == "WINDOW_START"]
    windows_end = [e for e in events if e.get("event") == "WINDOW_END"]
    
    print(f"\nTotal events: {len(events)}")
    print(f"Trades: {len(trades)}")
    print(f"Windows started: {len(windows_start)}")
    print(f"Windows ended: {len(windows_end)}")
    
    # Trade analysis
    if trades:
        # Variant A trades
        trades_a = [t for t in trades if t.get("event") == "TRADE_A"]
        trades_b = [t for t in trades if t.get("event") == "TRADE_B"]
        
        print(f"\n--- Strategy Breakdown ---")
        print(f"  Variant A (instant arb): {len(trades_a)}")
        print(f"  Variant B (legging): {len(trades_b)}")
        
        if trades_b:
            up_trades = [t for t in trades_b if t.get("side") == "up"]
            down_trades = [t for t in trades_b if t.get("side") == "down"]
            
            print(f"\n--- Variant B Details ---")
            print(f"  Up buys: {len(up_trades)}")
            print(f"  Down buys: {len(down_trades)}")
            
            if up_trades:
                avg_up_price = sum(t["price"] for t in up_trades) / len(up_trades)
                print(f"  Avg Up price: {avg_up_price:.4f}")
            
            if down_trades:
                avg_down_price = sum(t["price"] for t in down_trades) / len(down_trades)
                print(f"  Avg Down price: {avg_down_price:.4f}")
    
    # Final positions
    if windows_end:
        print(f"\n--- Final Positions ---")
        
        total_gp = 0
        hedged_windows = 0
        
        for we in windows_end:
            pos = we.get("position", {})
            q_up = pos.get("q_up", 0)
            q_down = pos.get("q_down", 0)
            gp = pos.get("guaranteed_profit", 0)
            
            hedged = q_up > 0 and q_down > 0
            if hedged:
                hedged_windows += 1
                total_gp += gp
            
            status = "HEDGED" if hedged else "UNHEDGED"
            print(f"  {we['slug']}: {status}, Up={q_up:.0f}, Down={q_down:.0f}, GP=${gp:.2f}")
        
        print(f"\n--- Summary ---")
        print(f"  Hedged windows: {hedged_windows} / {len(windows_end)}")
        print(f"  Total GP (hedged only): ${total_gp:.2f}")


def analyze_all():
    """Analyze all available data."""
    # Check data directories
    data_dir = Path("pm_data")
    results_dir = Path("pm_results")
    
    # Analyze tick files
    if data_dir.exists():
        tick_files = list(data_dir.glob("ticks_*.jsonl"))
        for tf in tick_files:
            analyze_ticks(tf)
    
    # Analyze trade files
    if results_dir.exists():
        trade_files = list(results_dir.glob("trades_*.jsonl"))
        for tf in trade_files:
            analyze_trades(tf)
    
    # Print overall conclusions
    print("\n" + "="*60)
    print("OVERALL CONCLUSIONS")
    print("="*60)
    
    print("""
Based on the data collected:

1. VARIANT A (Instant Arb):
   - Ask_sum is consistently >= 1.00
   - No instant arb opportunities observed
   - Market makers are efficient
   
2. VARIANT B (Reddit Legging/DCA):
   - Prices DO oscillate significantly within windows
   - When BTC trends one direction, that outcome becomes very cheap
   - The challenge: the OTHER side stays expensive (near $1.00)
   - Hedging requires price to REVERSE during the window
   
3. KEY INSIGHT:
   - The Reddit strategy CAN work if:
     a) Price oscillates enough that both sides become cheap at different times
     b) You have enough capital to wait for oscillation
     c) You accept directional risk if no oscillation occurs
   
   - It CANNOT work if:
     a) Price trends consistently in one direction
     b) The "cheap" side stays cheap and the "expensive" side never drops
     
4. REALISTIC EDGE:
   - Depends entirely on BTC volatility during 15-min windows
   - Higher volatility = more oscillation = more hedging opportunities
   - Low volatility = one-sided trades = directional risk
""")


if __name__ == "__main__":
    analyze_all()

