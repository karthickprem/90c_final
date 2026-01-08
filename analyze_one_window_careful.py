"""
Analyze ONE window carefully - get time calculation RIGHT
"""

import re

# Pick one window
WINDOW_FOLDER = "25_10_31_00_30_00_45"
file = f'backtesting15mbitcoin/market_logs/{WINDOW_FOLDER}/{WINDOW_FOLDER}.txt'

print("=" * 70)
print("SINGLE WINDOW ANALYSIS - CAREFUL")
print("=" * 70)
print(f"Window: {WINDOW_FOLDER}")
print()

# Parse folder name to get start/end times
# Format: 25_10_31_03_30_03_45
# Parts:  YY_MM_DD_HH_MM_HH_MM
#                   start   end
parts = WINDOW_FOLDER.split('_')
start_min = int(parts[4])  # Start minute (30)
end_min = int(parts[6])    # End minute (45)

print(f"Window timing:")
print(f"  Start: minute {start_min} (3:{start_min:02d} AM)")
print(f"  End:   minute {end_min} (3:{end_min:02d} AM)")
print(f"  Duration: {end_min - start_min} minutes")
print()

# Read file
lines = [l.strip() for l in open(file).readlines()]
print(f"Total ticks in file: {len(lines)}")
print()

# Parse each tick with CORRECT time calculation
ticks = []
for line in lines:
    match = re.match(r'(\d+):(\d+):\d+ - UP (\d+)C \| DOWN (\d+)C', line)
    if match:
        mins, secs, up, down = match.groups()
        
        # Convert to comparable format
        tick_time_secs = int(mins) * 60 + int(secs)
        end_time_secs = end_min * 60
        secs_left = end_time_secs - tick_time_secs
        
        ticks.append({
            'line': line,
            'timestamp': f"{mins}:{secs}",
            'secs_left': secs_left,
            'up': int(up),
            'down': int(down)
        })

print("=" * 70)
print("TIME CALCULATION CHECK")
print("=" * 70)
print("\nFirst tick:")
print(f"  {ticks[0]['timestamp']} -> {ticks[0]['secs_left']}s left ({ticks[0]['secs_left']//60}:{ticks[0]['secs_left']%60:02d})")
print(f"  Should be ~15:00 left")

print("\nMiddle tick:")
mid = ticks[len(ticks)//2]
print(f"  {mid['timestamp']} -> {mid['secs_left']}s left ({mid['secs_left']//60}:{mid['secs_left']%60:02d})")
print(f"  Should be ~7-8 min left")

print("\nLast tick:")
print(f"  {ticks[-1]['timestamp']} -> {ticks[-1]['secs_left']}s left ({ticks[-1]['secs_left']//60}:{ticks[-1]['secs_left']%60:02d})")
print(f"  Should be ~0:00 left")

# Find ticks in LAST 2 MINUTES (secs_left <= 120)
last_2min = [t for t in ticks if 0 <= t['secs_left'] <= 120]

print()
print("=" * 70)
print("LAST 2 MINUTES CHECK")
print("=" * 70)
print(f"Ticks in last 2 min (secs_left <= 120): {len(last_2min)}")
if last_2min:
    print(f"\nFirst tick of last 2 min:")
    print(f"  {last_2min[0]['timestamp']} ({last_2min[0]['secs_left']}s left)")
    print(f"  UP={last_2min[0]['up']}c, DOWN={last_2min[0]['down']}c")
    
    print(f"\nLast tick of last 2 min:")
    print(f"  {last_2min[-1]['timestamp']} ({last_2min[-1]['secs_left']}s left)")
    print(f"  UP={last_2min[-1]['up']}c, DOWN={last_2min[-1]['down']}c")

# Check for 90-98c in last 2 min
print()
print("=" * 70)
print("90-98c IN LAST 2 MIN")
print("=" * 70)

up_90_98 = [t for t in last_2min if 90 <= t['up'] <= 98]
down_90_98 = [t for t in last_2min if 90 <= t['down'] <= 98]

print(f"UP 90-98c:   {len(up_90_98)} ticks")
print(f"DOWN 90-98c: {len(down_90_98)} ticks")

if up_90_98:
    print(f"\nFirst UP 90-98c in last 2 min:")
    first = up_90_98[0]
    print(f"  {first['timestamp']} ({first['secs_left']}s left) - UP={first['up']}c DOWN={first['down']}c")

if down_90_98:
    print(f"\nFirst DOWN 90-98c in last 2 min:")
    first = down_90_98[0]
    print(f"  {first['timestamp']} ({first['secs_left']}s left) - UP={first['up']}c DOWN={first['down']}c")

# Determine winner
print()
print("=" * 70)
print("WINNER")
print("=" * 70)

max_up = max(t['up'] for t in ticks)
max_down = max(t['down'] for t in ticks)

print(f"UP peaked: {max_up}c")
print(f"DOWN peaked: {max_down}c")

winner = 'UP' if max_up > max_down else 'DOWN'
print(f"\nWINNER: {winner}")

# Check for reversal
print()
print("=" * 70)
print("REVERSAL CHECK")
print("=" * 70)

if up_90_98 and winner == 'DOWN':
    print("YES - REVERSAL!")
    print(f"  UP hit 90-98c in last 2 min ({len(up_90_98)} times)")
    print(f"  But DOWN won (peaked at {max_down}c)")
    print(f"  If you bought UP, you would have LOST")
elif down_90_98 and winner == 'UP':
    print("YES - REVERSAL!")
    print(f"  DOWN hit 90-98c in last 2 min ({len(down_90_98)} times)")
    print(f"  But UP won (peaked at {max_up}c)")
    print(f"  If you bought DOWN, you would have LOST")
else:
    print("NO REVERSAL")
    if up_90_98 and winner == 'UP':
        print(f"  UP hit 90-98c in last 2 min and UP won - SAFE TRADE")
    elif down_90_98 and winner == 'DOWN':
        print(f"  DOWN hit 90-98c in last 2 min and DOWN won - SAFE TRADE")
    else:
        print(f"  No 90-98c signal in last 2 min")

print("=" * 70)

