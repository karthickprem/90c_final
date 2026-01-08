"""Manually verify one reversal example"""

import re

file = 'backtesting15mbitcoin/market_logs/25_10_31_03_30_03_45/25_10_31_03_30_03_45.txt'

print("=" * 70)
print("MANUAL VERIFICATION - Reversal Example")
print("=" * 70)
print("Window: 25_10_31_03_30_03_45")
print("Expected: UP reached 91c, but DOWN won")
print("=" * 70)

lines = [l.strip() for l in open(file).readlines()]

ups = []
downs = []

for line in lines:
    match = re.match(r'(\d+):(\d+):\d+ - UP (\d+)C \| DOWN (\d+)C', line)
    if match:
        up = int(match.group(3))
        down = int(match.group(4))
        ups.append(up)
        downs.append(down)

print(f"\nTotal ticks: {len(ups)}")

print(f"\nUP prices:")
print(f"  Min: {min(ups)}c")
print(f"  Max: {max(ups)}c")

print(f"\nDOWN prices:")
print(f"  Min: {min(downs)}c")
print(f"  Max: {max(downs)}c")

print(f"\n" + "=" * 70)
print("VERIFICATION:")
print("=" * 70)

up_reached_90 = max(ups) >= 90
down_reached_90 = max(downs) >= 90

print(f"Did UP reach >= 90c? {up_reached_90} (max={max(ups)}c)")
print(f"Did DOWN reach >= 90c? {down_reached_90} (max={max(downs)}c)")

winner = "UP" if max(ups) > max(downs) else "DOWN"
print(f"\nWinner: {winner} (UP peaked {max(ups)}c, DOWN peaked {max(downs)}c)")

if up_reached_90 and winner == "DOWN":
    print("\n[CONFIRMED] REVERSAL: UP >= 90c but DOWN won")
elif down_reached_90 and winner == "UP":
    print("\n[CONFIRMED] REVERSAL: DOWN >= 90c but UP won")
else:
    print("\n[NOT A REVERSAL]")

print("=" * 70)





