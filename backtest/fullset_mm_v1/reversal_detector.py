"""
REVERSAL SPIKE DETECTOR

Goal: Find signals that predict when a 90c+ spike will REVERSE
instead of settling at 100c.

If we can detect reversals with >60% accuracy, we can:
1. Avoid bad entries
2. Trade the reversal (buy opposite side)
3. Short the failing spike
"""
from dataclasses import dataclass, field
from typing import List, Dict, Optional, Tuple
from collections import defaultdict
import json
import os

from .parse import find_window_ids, load_window_ticks
from .stream import merge_tick_streams, QuoteTick
from .config import DEFAULT_BUY_DIR, DEFAULT_SELL_DIR


@dataclass
class SpikeEvent:
    """A spike event with features for prediction."""
    window_id: str
    side: str  # "UP" or "DOWN"
    
    # Spike characteristics
    spike_price: int  # Price when crossing 90c
    spike_time: float  # Time in window
    time_remaining: float  # 900 - spike_time
    
    # FEATURES FOR PREDICTION
    # Speed features
    price_5s_ago: int  # Price 5 seconds before spike
    price_10s_ago: int  # Price 10 seconds before spike
    spike_speed_5s: float  # (spike_price - price_5s_ago) / 5
    spike_speed_10s: float
    
    # Momentum features
    momentum_direction: int  # +1 rising, -1 falling, 0 flat
    consecutive_up_ticks: int  # How many ticks in a row went up
    
    # Opposite side features
    opposite_price: int
    opposite_5s_ago: int
    opposite_trend: float  # Negative = falling (good), positive = rising (bad)
    
    # Spread features
    spread_at_spike: int  # ask - bid
    spread_5s_ago: int
    spread_widening: bool
    
    # Combined cost (full-set opportunity)
    combined_cost: int
    fullset_edge: int
    
    # OUTCOME
    final_price: int
    max_price_after: int
    min_price_after: int
    reversal_depth: int  # How much did it drop?
    settled_win: bool  # Did it reach 97+?
    
    @property
    def is_reversal(self) -> bool:
        """Did this spike reverse significantly (drop 10c+)?"""
        return self.reversal_depth >= 10
    
    @property
    def is_deep_reversal(self) -> bool:
        """Did this spike fail completely (drop 30c+)?"""
        return self.reversal_depth >= 30


def extract_spike_events(
    window_id: str,
    buy_dir: str,
    sell_dir: str,
    spike_threshold: int = 90
) -> List[SpikeEvent]:
    """Extract spike events with prediction features from a window."""
    buy_ticks, sell_ticks = load_window_ticks(window_id, buy_dir, sell_dir)
    if len(buy_ticks) < 10 or len(sell_ticks) < 10:
        return []
    
    merged = merge_tick_streams(buy_ticks, sell_ticks)
    if len(merged) < 20:
        return []
    
    events = []
    final = merged[-1]
    
    # Track history for feature calculation
    up_history = []  # (time, ask, bid)
    down_history = []
    
    up_spiked = False
    down_spiked = False
    
    for i, tick in enumerate(merged):
        t = tick.elapsed_secs
        
        # Add to history
        up_history.append((t, tick.up_ask, tick.up_bid))
        down_history.append((t, tick.down_ask, tick.down_bid))
        
        # Check for UP spike
        if not up_spiked and tick.up_ask >= spike_threshold:
            up_spiked = True
            event = _create_spike_event(
                window_id, "UP", tick, t, i, merged, 
                up_history, down_history, final, spike_threshold
            )
            if event:
                events.append(event)
        
        # Check for DOWN spike
        if not down_spiked and tick.down_ask >= spike_threshold:
            down_spiked = True
            event = _create_spike_event(
                window_id, "DOWN", tick, t, i, merged,
                down_history, up_history, final, spike_threshold
            )
            if event:
                events.append(event)
    
    return events


def _create_spike_event(
    window_id: str,
    side: str,
    tick: QuoteTick,
    time: float,
    idx: int,
    merged: List[QuoteTick],
    self_history: List,
    opp_history: List,
    final: QuoteTick,
    threshold: int
) -> Optional[SpikeEvent]:
    """Create a spike event with all features."""
    try:
        # Get prices based on side
        if side == "UP":
            spike_price = tick.up_ask
            spike_bid = tick.up_bid
            final_price = final.up_ask
            opp_price = tick.down_ask
        else:
            spike_price = tick.down_ask
            spike_bid = tick.down_bid
            final_price = final.down_ask
            opp_price = tick.up_ask
        
        # Find prices 5s and 10s ago
        price_5s_ago = spike_price
        price_10s_ago = spike_price
        spread_5s_ago = spike_price - spike_bid
        opp_5s_ago = opp_price
        
        for ht, hp, hb in reversed(self_history[:-1]):
            if time - ht >= 5 and price_5s_ago == spike_price:
                price_5s_ago = hp
                spread_5s_ago = hp - hb
            if time - ht >= 10:
                price_10s_ago = hp
                break
        
        for ht, hp, hb in reversed(opp_history[:-1]):
            if time - ht >= 5:
                opp_5s_ago = hp
                break
        
        # Calculate speeds
        speed_5s = (spike_price - price_5s_ago) / 5.0 if time >= 5 else 0
        speed_10s = (spike_price - price_10s_ago) / 10.0 if time >= 10 else 0
        
        # Momentum direction
        if len(self_history) >= 3:
            recent = [h[1] for h in self_history[-3:]]
            if recent[-1] > recent[-2] > recent[-3]:
                momentum = 1
            elif recent[-1] < recent[-2] < recent[-3]:
                momentum = -1
            else:
                momentum = 0
        else:
            momentum = 0
        
        # Consecutive up ticks
        consecutive_up = 0
        for j in range(len(self_history) - 2, -1, -1):
            if self_history[j+1][1] > self_history[j][1]:
                consecutive_up += 1
            else:
                break
        
        # Opposite trend
        opp_trend = opp_price - opp_5s_ago
        
        # Spread
        spread = spike_price - spike_bid
        spread_widening = spread > spread_5s_ago
        
        # Find max/min after spike
        remaining = merged[idx:]
        if side == "UP":
            prices_after = [m.up_ask for m in remaining]
        else:
            prices_after = [m.down_ask for m in remaining]
        
        max_after = max(prices_after) if prices_after else spike_price
        min_after = min(prices_after) if prices_after else spike_price
        reversal_depth = spike_price - min_after
        
        # Settlement
        settled_win = final_price >= 97
        
        return SpikeEvent(
            window_id=window_id,
            side=side,
            spike_price=spike_price,
            spike_time=time,
            time_remaining=900 - time,
            price_5s_ago=price_5s_ago,
            price_10s_ago=price_10s_ago,
            spike_speed_5s=speed_5s,
            spike_speed_10s=speed_10s,
            momentum_direction=momentum,
            consecutive_up_ticks=consecutive_up,
            opposite_price=opp_price,
            opposite_5s_ago=opp_5s_ago,
            opposite_trend=opp_trend,
            spread_at_spike=spread,
            spread_5s_ago=spread_5s_ago,
            spread_widening=spread_widening,
            combined_cost=spike_price + opp_price,
            fullset_edge=100 - (spike_price + opp_price),
            final_price=final_price,
            max_price_after=max_after,
            min_price_after=min_after,
            reversal_depth=reversal_depth,
            settled_win=settled_win
        )
    except Exception as e:
        return None


def analyze_reversal_predictors(events: List[SpikeEvent]):
    """Find which features predict reversals."""
    print("\n" + "="*70)
    print("REVERSAL PREDICTOR ANALYSIS")
    print("="*70)
    
    # Split into reversals vs non-reversals
    reversals = [e for e in events if e.is_reversal]
    non_reversals = [e for e in events if not e.is_reversal]
    
    print(f"\nTotal spike events: {len(events)}")
    print(f"Reversals (10c+ drop): {len(reversals)} ({len(reversals)/len(events)*100:.1f}%)")
    print(f"Non-reversals: {len(non_reversals)} ({len(non_reversals)/len(events)*100:.1f}%)")
    
    # Analyze each feature
    print("\n" + "-"*70)
    print("FEATURE ANALYSIS: Which features predict reversals?")
    print("-"*70)
    
    features = [
        ("Spike Speed (5s)", lambda e: e.spike_speed_5s, [0, 2, 5, 10]),
        ("Spike Speed (10s)", lambda e: e.spike_speed_10s, [0, 1, 2, 5]),
        ("Time Remaining", lambda e: e.time_remaining, [60, 180, 300, 600]),
        ("Opposite Trend", lambda e: e.opposite_trend, [-10, -5, 0, 5]),
        ("Spread at Spike", lambda e: e.spread_at_spike, [1, 2, 3, 5]),
        ("Consecutive Up Ticks", lambda e: e.consecutive_up_ticks, [0, 3, 5, 10]),
        ("Combined Cost", lambda e: e.combined_cost, [95, 98, 100, 102]),
    ]
    
    for name, getter, thresholds in features:
        print(f"\n{name}:")
        for i, thresh in enumerate(thresholds):
            if i == 0:
                filtered = [e for e in events if getter(e) <= thresh]
                label = f"  <= {thresh}"
            else:
                prev = thresholds[i-1]
                filtered = [e for e in events if prev < getter(e) <= thresh]
                label = f"  {prev}-{thresh}"
            
            if len(filtered) < 10:
                continue
            
            rev_count = sum(1 for e in filtered if e.is_reversal)
            rev_rate = rev_count / len(filtered) * 100
            
            # Is this predictive?
            baseline = len(reversals) / len(events) * 100
            lift = rev_rate - baseline
            
            indicator = ""
            if lift > 10:
                indicator = " <-- REVERSAL SIGNAL!"
            elif lift < -10:
                indicator = " <-- SAFE ENTRY"
            
            print(f"{label}: {len(filtered)} events, {rev_rate:.1f}% reverse{indicator}")
        
        # Also show extreme values
        high_end = [e for e in events if getter(e) > thresholds[-1]]
        if len(high_end) >= 10:
            rev_rate = sum(1 for e in high_end if e.is_reversal) / len(high_end) * 100
            print(f"  > {thresholds[-1]}: {len(high_end)} events, {rev_rate:.1f}% reverse")


def find_best_reversal_filters(events: List[SpikeEvent]):
    """Find combination of filters that best predict reversals."""
    print("\n" + "="*70)
    print("BEST REVERSAL PREDICTION RULES")
    print("="*70)
    
    baseline_rev_rate = sum(1 for e in events if e.is_reversal) / len(events)
    print(f"\nBaseline reversal rate: {baseline_rev_rate*100:.1f}%")
    
    rules = [
        # (name, filter_fn)
        ("Fast spike (speed > 3c/s)", lambda e: e.spike_speed_5s > 3),
        ("Very fast spike (speed > 5c/s)", lambda e: e.spike_speed_5s > 5),
        ("Opposite rising (trend > 0)", lambda e: e.opposite_trend > 0),
        ("Opposite rising fast (trend > 3)", lambda e: e.opposite_trend > 3),
        ("Wide spread (> 3c)", lambda e: e.spread_at_spike > 3),
        ("Early spike (> 5 min left)", lambda e: e.time_remaining > 300),
        ("Combined cost > 100 (no full-set)", lambda e: e.combined_cost > 100),
        ("Low momentum (< 3 up ticks)", lambda e: e.consecutive_up_ticks < 3),
        
        # Combinations
        ("Fast spike + opposite rising", 
         lambda e: e.spike_speed_5s > 3 and e.opposite_trend > 0),
        ("Fast spike + no full-set edge",
         lambda e: e.spike_speed_5s > 3 and e.combined_cost >= 100),
        ("Early + fast + wide spread",
         lambda e: e.time_remaining > 300 and e.spike_speed_5s > 3 and e.spread_at_spike > 2),
        ("Opposite rising + no full-set",
         lambda e: e.opposite_trend > 0 and e.combined_cost >= 100),
    ]
    
    print("\nRule Performance:")
    print("-"*70)
    print(f"{'Rule':<45} {'N':>6} {'Rev%':>8} {'Lift':>8}")
    print("-"*70)
    
    best_rules = []
    
    for name, filter_fn in rules:
        filtered = [e for e in events if filter_fn(e)]
        if len(filtered) < 20:
            continue
        
        rev_rate = sum(1 for e in filtered if e.is_reversal) / len(filtered)
        lift = (rev_rate - baseline_rev_rate) / baseline_rev_rate * 100
        
        best_rules.append((name, len(filtered), rev_rate, lift))
        
        indicator = ""
        if rev_rate > 0.5:
            indicator = " ***"
        elif rev_rate > 0.3:
            indicator = " **"
        
        print(f"{name:<45} {len(filtered):>6} {rev_rate*100:>7.1f}% {lift:>+7.0f}%{indicator}")
    
    # Find best rule for AVOIDING reversals (entry safety)
    print("\n" + "-"*70)
    print("SAFE ENTRY CONDITIONS (low reversal rate):")
    print("-"*70)
    
    safe_rules = [
        ("Slow spike (speed < 2c/s)", lambda e: e.spike_speed_5s < 2),
        ("Opposite falling (trend < -2)", lambda e: e.opposite_trend < -2),
        ("Narrow spread (< 2c)", lambda e: e.spread_at_spike < 2),
        ("Full-set available (cost < 98)", lambda e: e.combined_cost < 98),
        ("High momentum (5+ up ticks)", lambda e: e.consecutive_up_ticks >= 5),
        ("Late + slow + falling opposite",
         lambda e: e.time_remaining < 180 and e.spike_speed_5s < 2 and e.opposite_trend < 0),
        ("Full-set + slow spike",
         lambda e: e.combined_cost < 99 and e.spike_speed_5s < 3),
    ]
    
    for name, filter_fn in safe_rules:
        filtered = [e for e in events if filter_fn(e)]
        if len(filtered) < 20:
            continue
        
        rev_rate = sum(1 for e in filtered if e.is_reversal) / len(filtered)
        win_rate = sum(1 for e in filtered if e.settled_win) / len(filtered)
        
        print(f"{name}")
        print(f"  N={len(filtered)}, Reversal={rev_rate*100:.1f}%, Win={win_rate*100:.1f}%")


def build_reversal_score(events: List[SpikeEvent]):
    """Build a composite reversal risk score."""
    print("\n" + "="*70)
    print("REVERSAL RISK SCORE MODEL")
    print("="*70)
    print("""
Based on the analysis, here's a simple reversal risk scoring system:

REVERSAL RISK SCORE (higher = more likely to reverse)
------------------------------------------------------
+2 points: Spike speed > 5c/s (too fast)
+2 points: Opposite side rising (trend > 0)
+1 point:  Wide spread (> 3c)
+1 point:  Early in window (> 5 min left)
+1 point:  No full-set edge (cost >= 100)
+1 point:  Low momentum (< 3 consecutive up ticks)

-2 points: Opposite side falling fast (trend < -5)
-1 point:  Full-set edge available (cost < 98)
-1 point:  Narrow spread (< 2c)
-1 point:  High momentum (5+ consecutive up ticks)

DECISION RULE:
  Score <= 0: SAFE TO ENTER (low reversal risk)
  Score 1-2:  CAUTION (moderate risk)
  Score 3-4:  HIGH RISK (consider avoiding)
  Score >= 5: REVERSAL LIKELY (trade the reversal!)
""")
    
    # Test the scoring system
    print("\nTesting Score Model on Data:")
    print("-"*50)
    
    def compute_score(e: SpikeEvent) -> int:
        score = 0
        if e.spike_speed_5s > 5:
            score += 2
        if e.opposite_trend > 0:
            score += 2
        if e.spread_at_spike > 3:
            score += 1
        if e.time_remaining > 300:
            score += 1
        if e.combined_cost >= 100:
            score += 1
        if e.consecutive_up_ticks < 3:
            score += 1
        
        if e.opposite_trend < -5:
            score -= 2
        if e.combined_cost < 98:
            score -= 1
        if e.spread_at_spike < 2:
            score -= 1
        if e.consecutive_up_ticks >= 5:
            score -= 1
        
        return score
    
    score_buckets = defaultdict(lambda: {"total": 0, "reversals": 0, "wins": 0})
    
    for e in events:
        score = compute_score(e)
        bucket = min(max(score, -2), 6)  # Clamp to -2 to 6
        score_buckets[bucket]["total"] += 1
        if e.is_reversal:
            score_buckets[bucket]["reversals"] += 1
        if e.settled_win:
            score_buckets[bucket]["wins"] += 1
    
    print(f"{'Score':<8} {'Count':>8} {'Reversal%':>12} {'Win%':>10} {'Action'}")
    print("-"*50)
    
    for score in sorted(score_buckets.keys()):
        stats = score_buckets[score]
        rev_rate = stats["reversals"] / stats["total"] * 100
        win_rate = stats["wins"] / stats["total"] * 100
        
        if score <= 0:
            action = "ENTER"
        elif score <= 2:
            action = "CAUTION"
        elif score <= 4:
            action = "AVOID"
        else:
            action = "FADE"
        
        print(f"{score:<8} {stats['total']:>8} {rev_rate:>11.1f}% {win_rate:>9.1f}% {action}")


def main():
    """Run complete reversal analysis."""
    print("="*70)
    print("REVERSAL SPIKE DETECTOR - FINDING THE EDGE")
    print("="*70)
    
    buy_ids = set(find_window_ids(DEFAULT_BUY_DIR))
    sell_ids = set(find_window_ids(DEFAULT_SELL_DIR))
    common = sorted(buy_ids & sell_ids)
    
    print(f"\nAnalyzing {len(common)} windows for spike patterns...")
    
    all_events = []
    for i, wid in enumerate(common):
        if i % 500 == 0:
            print(f"  Processing {i}/{len(common)}...")
        events = extract_spike_events(wid, DEFAULT_BUY_DIR, DEFAULT_SELL_DIR)
        all_events.extend(events)
    
    print(f"\nExtracted {len(all_events)} spike events")
    
    analyze_reversal_predictors(all_events)
    find_best_reversal_filters(all_events)
    build_reversal_score(all_events)
    
    print("\n" + "="*70)
    print("TRADING IMPLICATIONS")
    print("="*70)
    print("""
IF YOU CAN DETECT REVERSALS:

1. AVOID BAD ENTRIES
   - When reversal score >= 3, don't enter
   - This alone could flip the strategy from -EV to +EV

2. TRADE THE REVERSAL (Advanced)
   - When reversal score >= 5:
     * The spike side is overpriced
     * The opposite side is underpriced
   - BUY THE OPPOSITE SIDE at the cheap price
   - If reversal happens, opposite side goes from 10c -> 50c+

3. FADE THE SPIKE (Very Advanced)
   - When reversal score >= 5 AND spread is wide:
     * SELL the spiking side (if possible)
     * Buy it back after reversal at lower price
   - This requires being able to short (not always possible)

4. COMBINE WITH FULL-SET
   - Best entries: Full-set available AND low reversal score
   - This gives you BOTH guaranteed edge AND directional confirmation
""")


if __name__ == "__main__":
    main()

