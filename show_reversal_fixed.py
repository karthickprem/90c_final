"""Show actual reversal with CORRECT time calculation"""

import re

file = 'backtesting15mbitcoin/market_logs/25_10_31_03_30_03_45/25_10_31_03_30_03_45.txt'

print("=" * 70)
print("REVERSAL EXAMPLE")
print("=" * 70)
print("Window: 25_10_31_03_30_03_45")
print("Time: 3:30 AM to 3:45 AM (15 minutes)")
print("=" * 70)

lines = [l.strip() for l in open(file).readlines()]

# Parse with CORRECT time calculation
# Folder: 25_10_31_03_30_03_45 means start=30, end=45
start_min = 30
end_min = 45

ticks = []
for line in lines:
    match = re.match(r'(\d+):(\d+):\d+ - UP (\d+)C \| DOWN (\d+)C', line)
    if match:
        mins, secs, up, down = match.groups()
        current_time_secs = int(mins) * 60 + int(secs)
        end_time_secs = end_min * 60
        secs_left = end_time_secs - current_time_secs
        
        ticks.append({
            'time': f"{mins}:{secs}",
            'secs_left': secs_left,
            'up': int(up),
            'down': int(down)
        })

print(f"\nTotal ticks: {len(ticks)}")

# Show key moments
print(f"\n" + "=" * 70)
print("KEY MOMENTS")
print("=" * 70)

print(f"\nSTART (15:00 left):")
print(f"  {ticks[0]['time']} - UP={ticks[0]['up']}c DOWN={ticks[0]['down']}c")

print(f"\nMIDPOINT (~7:30 left):")
mid_idx = len(ticks) // 2
print(f"  {ticks[mid_idx]['time']} - UP={ticks[mid_idx]['up']}c DOWN={ticks[mid_idx]['down']}c")

# Find when UP first hits 90+ in last 2 min
last_2min = [t for t in ticks if 0 <= t['secs_left'] <= 120]
up_90_last_2min = [t for t in last_2min if t['up'] >= 90]
down_90_last_2min = [t for t in last_2min if t['down'] >= 90]

if up_90_last_2min:
    first = up_90_last_2min[0]
    print(f"\n" + "=" * 70)
    print(f"UP HITS 90c IN LAST 2 MIN:")
    print("=" * 70)
    print(f"  Time: {first['time']}")
    print(f"  Time left: {first['secs_left']}s ({first['secs_left']//60}:{first['secs_left']%60:02d})")
    print(f"  UP={first['up']}c, DOWN={first['down']}c")
    print(f"\n  >>> ENTRY SIGNAL: BUY UP @ {first['up']}c <<<")
    
    # Show what happened after
    print(f"\n  AFTER ENTRY (next 20 ticks):")
    entry_idx = ticks.index(first)
    for i in range(entry_idx, min(entry_idx + 20, len(ticks))):
        t = ticks[i]
        marker = " <-- YOU BOUGHT HERE" if i == entry_idx else ""
        print(f"    [{t['secs_left']:3}s] UP={t['up']:2}c DOWN={t['down']:2}c{marker}")

print(f"\n" + "=" * 70)
print("FINAL RESULT")
print("=" * 70)

max_up = max(t['up'] for t in ticks)
max_down = max(t['down'] for t in ticks)

print(f"UP peaked: {max_up}c")
print(f"DOWN peaked: {max_down}c")
winner = 'UP' if max_up > max_down else 'DOWN'
print(f"\nWINNER: {winner}")

if up_90_last_2min:
    if winner == 'DOWN':
        print(f"\n>>> REVERSAL CONFIRMED <<<")
        print(f"  You bought UP @ {up_90_last_2min[0]['up']}c")
        print(f"  But DOWN reversed and won at {max_down}c")
        print(f"  LOSS: -$6.30 (assuming 70% of $10)")
    else:
        print(f"\n>>> NO REVERSAL - You won! <<<")

print("=" * 70)





