"""
Show detailed tick-by-tick data for a reversal window
"""

import re

# Example reversal: UP touched 91c in last 2min but DOWN won
file = 'backtesting15mbitcoin/market_logs/25_10_31_03_30_03_45/25_10_31_03_30_03_45.txt'

print("=" * 70)
print("REVERSAL EXAMPLE: UP touched 91c but DOWN won")
print("=" * 70)
print("Window: 25_10_31_03_30_03_45")
print("Date: October 31, 2025, 3:30 AM - 3:45 AM")
print("=" * 70)

lines = [l.strip() for l in open(file).readlines()]

# Parse all ticks
ticks = []
for line in lines:
    match = re.match(r'(\d+):(\d+):\d+ - UP (\d+)C \| DOWN (\d+)C', line)
    if match:
        mins, secs, up, down = match.groups()
        total_secs = int(mins) * 60 + int(secs)
        secs_left = 900 - total_secs
        ticks.append({
            'time': f"{mins}:{secs}",
            'secs_left': secs_left,
            'up': int(up),
            'down': int(down)
        })

print(f"\nTotal ticks: {len(ticks)}")
print()

# Find when UP first hit 90+ in last 2 min
last_2min_ticks = [t for t in ticks if t['secs_left'] <= 120]
up_90_in_last_2min = [t for t in last_2min_ticks if t['up'] >= 90]

print("=" * 70)
print("LAST 2 MINUTES (secs_left <= 120)")
print("=" * 70)

if up_90_in_last_2min:
    first_90 = up_90_in_last_2min[0]
    print(f"\nUP FIRST hit 90c+ at:")
    print(f"  Time: {first_90['time']} (elapsed)")
    print(f"  Time left: {first_90['secs_left']}s ({first_90['secs_left']//60}:{first_90['secs_left']%60:02d})")
    print(f"  UP={first_90['up']}c, DOWN={first_90['down']}c")
    print(f"\n  >>> IF YOU BOUGHT UP HERE @ {first_90['up']}c <<<")

print(f"\n" + "-" * 70)
print("WHAT HAPPENED AFTER (Next 10 ticks):")
print("-" * 70)

# Find index of first 90c
if up_90_in_last_2min:
    first_90_idx = ticks.index(first_90)
    
    for i in range(first_90_idx, min(first_90_idx + 15, len(ticks))):
        t = ticks[i]
        marker = " <-- ENTRY" if i == first_90_idx else ""
        print(f"  [{t['secs_left']:3}s left] UP={t['up']:2}c DOWN={t['down']:2}c{marker}")

print()
print("=" * 70)
print("FINAL OUTCOME")
print("=" * 70)

max_up = max(t['up'] for t in ticks)
max_down = max(t['down'] for t in ticks)

print(f"UP peaked at: {max_up}c")
print(f"DOWN peaked at: {max_down}c")
print(f"\nWinner: {'UP' if max_up > max_down else 'DOWN'}")

if max_up > max_down:
    print(f"\n>>> RESULT: You would have WON")
else:
    print(f"\n>>> RESULT: REVERSAL - You would have LOST")
    print(f"    UP hit {first_90['up']}c in last 2 min")
    print(f"    But DOWN came back and reached {max_down}c")
    print(f"    You lost ${first_90['up']*0.70:.2f} (70% position)")

print("=" * 70)





