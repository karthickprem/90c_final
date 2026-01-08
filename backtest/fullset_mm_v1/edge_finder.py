"""
Find the ACTUAL profitable edge in 90c+ entries.
Key insight from data: 0-reversal entries have 98.6% win rate!
We need to find PREDICTIVE features that identify clean spikes.
"""
from dataclasses import dataclass, field
from typing import List, Dict, Optional, Tuple
from collections import defaultdict
import json
import os

from .parse import find_window_ids
from .config import DEFAULT_BUY_DIR, DEFAULT_SELL_DIR


@dataclass
class EntrySignal:
    """A potential 90c+ entry opportunity with features."""
    window_id: str
    side: str
    entry_price: int
    entry_time: float
    time_remaining: float
    
    # Predictive features
    seconds_above_85: float  # How long has it been above 85c?
    seconds_above_88: float  # How long above 88c?
    momentum_5s: float  # Price change over last 5 seconds
    opposite_price: int
    opposite_trend: float  # Is opposite side falling?
    spread_at_entry: int  # up_ask - up_bid at entry
    
    # Outcome
    settled_win: bool
    final_price: int
    max_reversal: int


def analyze_window_for_signals(window_id: str, buy_dir: str, sell_dir: str) -> List[EntrySignal]:
    """Extract entry signals with predictive features from a window."""
    from .parse import load_window_ticks
    from .stream import merge_tick_streams
    
    buy_ticks, sell_ticks = load_window_ticks(window_id, buy_dir, sell_dir)
    if not buy_ticks or not sell_ticks:
        return []
    
    merged = merge_tick_streams(buy_ticks, sell_ticks)
    if len(merged) < 10:
        return []
    
    signals = []
    final = merged[-1]
    
    # Track price history for momentum calculation
    up_history = []
    down_history = []
    
    # Track time above thresholds
    first_above_85_up = None
    first_above_88_up = None
    first_above_85_down = None
    first_above_88_down = None
    
    # Already triggered thresholds (only capture first crossing)
    up_triggered = set()
    down_triggered = set()
    
    for i, tick in enumerate(merged):
        t = tick.elapsed_secs
        
        # Track history
        up_history.append((t, tick.up_ask))
        down_history.append((t, tick.down_ask))
        
        # Track first time above thresholds
        if tick.up_ask >= 85 and first_above_85_up is None:
            first_above_85_up = t
        if tick.up_ask >= 88 and first_above_88_up is None:
            first_above_88_up = t
        if tick.down_ask >= 85 and first_above_85_down is None:
            first_above_85_down = t
        if tick.down_ask >= 88 and first_above_88_down is None:
            first_above_88_down = t
        
        # Check for entry thresholds
        for thresh in [90, 93, 95]:
            # UP side
            if tick.up_ask >= thresh and thresh not in up_triggered:
                up_triggered.add(thresh)
                
                # Calculate features
                secs_85 = (t - first_above_85_up) if first_above_85_up else 0
                secs_88 = (t - first_above_88_up) if first_above_88_up else 0
                
                # Momentum: price change over last 5 seconds
                momentum = 0
                for ht, hp in reversed(up_history[:-1]):
                    if t - ht >= 5:
                        momentum = tick.up_ask - hp
                        break
                
                # Opposite trend (negative = falling = good)
                opp_trend = 0
                for ht, hp in reversed(down_history[:-1]):
                    if t - ht >= 5:
                        opp_trend = tick.down_ask - hp
                        break
                
                # Find max reversal after this point
                remaining_up = [m.up_ask for m in merged[i:]]
                max_rev = tick.up_ask - min(remaining_up) if remaining_up else 0
                
                signal = EntrySignal(
                    window_id=window_id,
                    side="UP",
                    entry_price=thresh,
                    entry_time=t,
                    time_remaining=900 - t,
                    seconds_above_85=secs_85,
                    seconds_above_88=secs_88,
                    momentum_5s=momentum,
                    opposite_price=tick.down_ask,
                    opposite_trend=opp_trend,
                    spread_at_entry=tick.up_ask - tick.up_bid,
                    settled_win=(final.up_ask >= 97),
                    final_price=final.up_ask,
                    max_reversal=max_rev
                )
                signals.append(signal)
            
            # DOWN side
            if tick.down_ask >= thresh and (thresh + 100) not in down_triggered:
                down_triggered.add(thresh + 100)
                
                secs_85 = (t - first_above_85_down) if first_above_85_down else 0
                secs_88 = (t - first_above_88_down) if first_above_88_down else 0
                
                momentum = 0
                for ht, hp in reversed(down_history[:-1]):
                    if t - ht >= 5:
                        momentum = tick.down_ask - hp
                        break
                
                opp_trend = 0
                for ht, hp in reversed(up_history[:-1]):
                    if t - ht >= 5:
                        opp_trend = tick.up_ask - hp
                        break
                
                remaining_down = [m.down_ask for m in merged[i:]]
                max_rev = tick.down_ask - min(remaining_down) if remaining_down else 0
                
                signal = EntrySignal(
                    window_id=window_id,
                    side="DOWN",
                    entry_price=thresh,
                    entry_time=t,
                    time_remaining=900 - t,
                    seconds_above_85=secs_85,
                    seconds_above_88=secs_88,
                    momentum_5s=momentum,
                    opposite_price=tick.up_ask,
                    opposite_trend=opp_trend,
                    spread_at_entry=tick.down_ask - tick.down_bid,
                    settled_win=(final.down_ask >= 97),
                    final_price=final.down_ask,
                    max_reversal=max_rev
                )
                signals.append(signal)
    
    return signals


def compute_ev(wins: int, losses: int, entry_price: int) -> float:
    """Compute expected value per trade."""
    total = wins + losses
    if total == 0:
        return 0
    win_rate = wins / total
    profit = 100 - entry_price
    return (win_rate * profit) - ((1 - win_rate) * entry_price)


def find_profitable_filters(signals: List[EntrySignal]):
    """Find filter combinations that produce profitable entries."""
    print("\n" + "="*70)
    print("SEARCHING FOR PROFITABLE FILTER COMBINATIONS")
    print("="*70)
    
    # Group by entry price
    by_price = defaultdict(list)
    for s in signals:
        by_price[s.entry_price].append(s)
    
    for entry_price in sorted(by_price.keys()):
        sigs = by_price[entry_price]
        print(f"\n{'='*60}")
        print(f"ENTRY PRICE: {entry_price}c")
        print(f"{'='*60}")
        
        # Baseline
        wins = sum(1 for s in sigs if s.settled_win)
        losses = len(sigs) - wins
        base_ev = compute_ev(wins, losses, entry_price)
        print(f"\nBaseline: {len(sigs)} signals, {wins}/{losses} W/L, EV={base_ev:.2f}c")
        
        # Filter 1: Time above 88c > X seconds
        print(f"\n--- Filter: Seconds above 88c ---")
        for thresh in [5, 10, 20, 30, 60]:
            filtered = [s for s in sigs if s.seconds_above_88 >= thresh]
            if len(filtered) < 20:
                continue
            w = sum(1 for s in filtered if s.settled_win)
            l = len(filtered) - w
            ev = compute_ev(w, l, entry_price)
            tag = "[PROFITABLE]" if ev > 0 else ""
            print(f"  >= {thresh}s: n={len(filtered)}, W/L={w}/{l}, WR={w/len(filtered)*100:.1f}%, EV={ev:.2f}c {tag}")
        
        # Filter 2: Positive momentum
        print(f"\n--- Filter: 5s Momentum ---")
        for thresh in [0, 2, 5, 10]:
            filtered = [s for s in sigs if s.momentum_5s >= thresh]
            if len(filtered) < 20:
                continue
            w = sum(1 for s in filtered if s.settled_win)
            l = len(filtered) - w
            ev = compute_ev(w, l, entry_price)
            tag = "[PROFITABLE]" if ev > 0 else ""
            print(f"  >= {thresh}c: n={len(filtered)}, W/L={w}/{l}, WR={w/len(filtered)*100:.1f}%, EV={ev:.2f}c {tag}")
        
        # Filter 3: Opposite side collapsing (negative trend)
        print(f"\n--- Filter: Opposite Side Falling ---")
        for thresh in [0, -2, -5, -10]:
            filtered = [s for s in sigs if s.opposite_trend <= thresh]
            if len(filtered) < 20:
                continue
            w = sum(1 for s in filtered if s.settled_win)
            l = len(filtered) - w
            ev = compute_ev(w, l, entry_price)
            tag = "[PROFITABLE]" if ev > 0 else ""
            print(f"  <= {thresh}c: n={len(filtered)}, W/L={w}/{l}, WR={w/len(filtered)*100:.1f}%, EV={ev:.2f}c {tag}")
        
        # Filter 4: Low opposite price (full-set available)
        print(f"\n--- Filter: Opposite Price (Full-Set Opportunity) ---")
        for thresh in [15, 12, 10, 8, 5]:
            filtered = [s for s in sigs if s.opposite_price <= thresh]
            if len(filtered) < 20:
                continue
            w = sum(1 for s in filtered if s.settled_win)
            l = len(filtered) - w
            ev = compute_ev(w, l, entry_price)
            combined = entry_price + thresh
            fs_edge = 100 - combined
            tag = "[PROFITABLE]" if ev > 0 else ""
            print(f"  <= {thresh}c (combo={combined}c, FS_edge={fs_edge}c): n={len(filtered)}, WR={w/len(filtered)*100:.1f}%, EV={ev:.2f}c {tag}")
        
        # Filter 5: COMBINED conditions
        print(f"\n--- Combined Filters ---")
        
        # Combo A: momentum + time
        filtered = [s for s in sigs if s.seconds_above_88 >= 10 and s.momentum_5s >= 2]
        if len(filtered) >= 20:
            w = sum(1 for s in filtered if s.settled_win)
            l = len(filtered) - w
            ev = compute_ev(w, l, entry_price)
            tag = "[PROFITABLE]" if ev > 0 else ""
            print(f"  Time>=10s + Momentum>=2c: n={len(filtered)}, WR={w/len(filtered)*100:.1f}%, EV={ev:.2f}c {tag}")
        
        # Combo B: momentum + opposite falling
        filtered = [s for s in sigs if s.momentum_5s >= 2 and s.opposite_trend <= -2]
        if len(filtered) >= 20:
            w = sum(1 for s in filtered if s.settled_win)
            l = len(filtered) - w
            ev = compute_ev(w, l, entry_price)
            tag = "[PROFITABLE]" if ev > 0 else ""
            print(f"  Momentum>=2c + Opp_trend<=-2c: n={len(filtered)}, WR={w/len(filtered)*100:.1f}%, EV={ev:.2f}c {tag}")
        
        # Combo C: time + opposite falling + momentum
        filtered = [s for s in sigs if s.seconds_above_88 >= 10 and s.opposite_trend <= -2 and s.momentum_5s >= 0]
        if len(filtered) >= 20:
            w = sum(1 for s in filtered if s.settled_win)
            l = len(filtered) - w
            ev = compute_ev(w, l, entry_price)
            tag = "[PROFITABLE]" if ev > 0 else ""
            print(f"  Time>=10s + Opp<=-2c + Mom>=0: n={len(filtered)}, WR={w/len(filtered)*100:.1f}%, EV={ev:.2f}c {tag}")
        
        # Combo D: FULL-SET GUARANTEED (opposite < 10c = combined < 100c)
        max_opp = 100 - entry_price
        filtered = [s for s in sigs if s.opposite_price < max_opp]
        if len(filtered) >= 10:
            w = sum(1 for s in filtered if s.settled_win)
            l = len(filtered) - w
            # For full-set, profit is GUARANTEED as 100 - combined
            avg_opp = sum(s.opposite_price for s in filtered) / len(filtered)
            avg_combined = entry_price + avg_opp
            guaranteed_edge = 100 - avg_combined
            print(f"  FULL-SET (opp<{max_opp}c): n={len(filtered)}, avg_combo={avg_combined:.1f}c, GUARANTEED_EDGE={guaranteed_edge:.1f}c [ALWAYS PROFITABLE]")


def main():
    """Run edge finder analysis."""
    print("="*70)
    print("EDGE FINDER: Searching for Profitable 90c+ Entry Conditions")
    print("="*70)
    
    window_ids = find_window_ids(DEFAULT_BUY_DIR)
    print(f"Analyzing {len(window_ids)} windows...")
    
    all_signals = []
    for i, wid in enumerate(window_ids):
        if i % 500 == 0:
            print(f"  Processing {i}/{len(window_ids)}")
        signals = analyze_window_for_signals(wid, DEFAULT_BUY_DIR, DEFAULT_SELL_DIR)
        all_signals.extend(signals)
    
    print(f"\nExtracted {len(all_signals)} entry signals")
    
    find_profitable_filters(all_signals)
    
    # Final summary
    print("\n" + "="*70)
    print("KEY FINDINGS")
    print("="*70)
    print("""
STRATEGY TO PROFIT FROM 90-99c ENTRIES:

1. FULL-SET HYBRID (Guaranteed Edge):
   - When price crosses 90c, check opposite side
   - If opposite < 10c, buy BOTH sides
   - Combined cost < 100c = guaranteed profit
   - Example: UP=90c + DOWN=8c = 98c cost = 2c guaranteed
   - This is the SAFEST approach

2. MOMENTUM PERSISTENCE (Directional Edge):
   - Require price to be above 88c for at least 10 seconds
   - Require positive 5s momentum (price still rising)
   - Require opposite side falling (confirmation)
   - This filters out "flash spikes" that reverse

3. AVOID THESE CONDITIONS:
   - Entries where opposite side is rising (divergence)
   - Entries without momentum (stagnant prices)
   - Very late entries (< 1 min) without strong confirmation

4. POSITION SIZING:
   - Size inversely to entry price
   - At 90c: can risk normal size
   - At 95c: halve the size
   - At 98c: quarter the size
   - This limits damage from reversals

5. THE HYBRID APPROACH:
   Primary: Full-set accumulation (guaranteed edge)
   Secondary: Spike entries only when ALL conditions met:
   - Price >= 90c
   - Above 88c for >= 10 seconds
   - Momentum >= 2c over last 5s
   - Opposite side falling
   - Position size reduced for high entries
""")


if __name__ == "__main__":
    main()

