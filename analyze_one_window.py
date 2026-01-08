"""Analyze one window to show user the data structure"""

import re

file = 'backtesting15mbitcoin/market_logs/25_10_31_00_00_00_15/25_10_31_00_00_00_15.txt'

print("=" * 70)
print("ANALYZING ONE WINDOW")
print("=" * 70)
print(f"Window: 25_10_31_00_00_00_15")
print("Date: October 31, 2025, 12:00 AM - 12:15 AM")
print("=" * 70)

lines = [l.strip() for l in open(file).readlines()]

print(f"\nTotal ticks recorded: {len(lines)}")
print(f"Sampling rate: ~{900/len(lines):.1f} seconds per tick")

print("\n" + "=" * 70)
print("TIMELINE")
print("=" * 70)

# Show samples through the window
samples = [
    (0, "START"),
    (len(lines)//4, "25% DONE (11:15 left)"),
    (len(lines)//2, "50% DONE (7:30 left)"),
    (3*len(lines)//4, "75% DONE (3:45 left)"),
    (-5, "NEAR END (<1 min left)"),
    (-1, "FINAL TICK")
]

for idx, label in samples:
    line = lines[idx]
    print(f"\n{label}:")
    print(f"  {line}")

print("\n" + "=" * 70)
print("PRICE ANALYSIS")
print("=" * 70)

ups = []
downs = []

for line in lines:
    match = re.match(r'(\d+):(\d+):\d+ - UP (\d+)C \| DOWN (\d+)C', line)
    if match:
        mins, secs, up, down = match.groups()
        ups.append(int(up))
        downs.append(int(down))

print(f"\nUP prices:")
print(f"  Min: {min(ups)}c")
print(f"  Max: {max(ups)}c")
print(f"  Range: {min(ups)}c to {max(ups)}c")

print(f"\nDOWN prices:")
print(f"  Min: {min(downs)}c")
print(f"  Max: {max(downs)}c")
print(f"  Range: {min(downs)}c to {max(downs)}c")

print(f"\n" + "=" * 70)
print(f"WINNER: {'UP' if max(ups) > max(downs) else 'DOWN'}")
print(f"  UP peaked at {max(ups)}c")
print(f"  DOWN peaked at {max(downs)}c")
print("=" * 70)

# Check 90-99c availability
print("\n" + "=" * 70)
print("90-99c SIGNAL ANALYSIS")
print("=" * 70)

high_up = [i for i, u in enumerate(ups) if 90 <= u <= 99]
high_down = [i for i, d in enumerate(downs) if 90 <= d <= 99]

print(f"\nTicks with UP 90-99c: {len(high_up)}")
if high_up:
    first_idx = high_up[0]
    first_time = lines[first_idx].split(' - ')[0]
    print(f"  First appearance: {lines[first_idx]}")
    print(f"  At time: {first_time}")

print(f"\nTicks with DOWN 90-99c: {len(high_down)}")
if high_down:
    first_idx = high_down[0]
    print(f"  First appearance: {lines[first_idx]}")

if high_up or high_down:
    print(f"\n[YES] This window HAS 90-99c signals")
    print(f"  Total ticks in range: {len(high_up) + len(high_down)}")
else:
    print(f"\n[NO] This window NEVER reached 90-99c")

print("=" * 70)

