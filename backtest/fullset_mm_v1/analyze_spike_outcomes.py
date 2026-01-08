"""Analyze actual spike outcomes by reversal score."""
from collections import defaultdict

from .reversal_detector import extract_spike_events
from .reversal_strategy import compute_reversal_score
from .parse import find_window_ids
from .config import DEFAULT_BUY_DIR, DEFAULT_SELL_DIR


def main():
    buy_ids = set(find_window_ids(DEFAULT_BUY_DIR))
    sell_ids = set(find_window_ids(DEFAULT_SELL_DIR))
    common = sorted(buy_ids & sell_ids)
    
    print("Analyzing spike outcomes by reversal score...")
    print()
    
    all_events = []
    for i, wid in enumerate(common):
        if i % 1000 == 0:
            print(f"  {i}/{len(common)}...")
        events = extract_spike_events(wid, DEFAULT_BUY_DIR, DEFAULT_SELL_DIR, 90)
        all_events.extend(events)
    
    print(f"\nTotal spikes: {len(all_events)}")
    print()
    
    # Analyze by score
    score_stats = defaultdict(lambda: {"total": 0, "spike_wins": 0, "opp_wins": 0})
    
    for e in all_events:
        score = compute_reversal_score(
            e.spike_speed_5s, e.opposite_trend, e.spread_at_spike,
            e.time_remaining, e.combined_cost, e.consecutive_up_ticks
        )
        
        bucket = min(max(score, -2), 6)
        score_stats[bucket]["total"] += 1
        
        # Did spike side win (settle at 97+)?
        if e.settled_win:
            score_stats[bucket]["spike_wins"] += 1
        else:
            score_stats[bucket]["opp_wins"] += 1
    
    print("SPIKE WIN RATE BY REVERSAL SCORE")
    print("=" * 70)
    print(f"Score    Total    Spike Wins   Opp Wins    Spike%    Opp%")
    print("-" * 70)
    
    for score in sorted(score_stats.keys()):
        s = score_stats[score]
        spike_rate = s["spike_wins"] / s["total"] * 100 if s["total"] > 0 else 0
        opp_rate = s["opp_wins"] / s["total"] * 100 if s["total"] > 0 else 0
        print(f"{score:<8} {s['total']:<8} {s['spike_wins']:<12} {s['opp_wins']:<11} {spike_rate:.1f}%     {opp_rate:.1f}%")
    
    print()
    print("=" * 70)
    print("BREAK-EVEN ANALYSIS")
    print("=" * 70)
    print("""
For a FADE trade (buy opposite at ~10c):
  - If opposite wins: profit = (100 - 10) = 90c
  - If spike wins: loss = 10c
  - Break-even: need 10/(10+90) = 10% opposite win rate

For a DIRECTIONAL trade (buy spike at 90c):
  - If spike wins: profit = (100 - 90) = 10c
  - If opposite wins: loss = 90c
  - Break-even: need 90/(90+10) = 90% spike win rate
""")
    
    # Find which scores are actually tradeable
    print("TRADEABLE SIGNALS:")
    print("-" * 70)
    
    for score in sorted(score_stats.keys()):
        s = score_stats[score]
        if s["total"] < 20:
            continue
        
        spike_rate = s["spike_wins"] / s["total"] * 100
        opp_rate = s["opp_wins"] / s["total"] * 100
        
        # Check fade viability
        if opp_rate > 10:
            fade_ev = (opp_rate/100 * 90) - ((100-opp_rate)/100 * 10)
            print(f"Score {score}: Opp wins {opp_rate:.1f}% -> FADE EV = {fade_ev:.1f}c (PROFITABLE!)" if fade_ev > 0 
                  else f"Score {score}: Opp wins {opp_rate:.1f}% -> FADE EV = {fade_ev:.1f}c (not enough)")
        
        # Check directional viability  
        if spike_rate > 90:
            dir_ev = (spike_rate/100 * 10) - ((100-spike_rate)/100 * 90)
            print(f"Score {score}: Spike wins {spike_rate:.1f}% -> DIRECTIONAL EV = {dir_ev:.1f}c (PROFITABLE!)" if dir_ev > 0
                  else f"Score {score}: Spike wins {spike_rate:.1f}% -> DIRECTIONAL EV = {dir_ev:.1f}c (not enough)")


if __name__ == "__main__":
    main()

