"""
Analyze ALL windows properly:
- 90-98c in last 2 minutes only
- Track UP->UP win, DOWN->DOWN win, reversals
- Track early settlements (before last 2 min)
"""

import os
import re

folders = sorted(os.listdir('backtesting15mbitcoin/market_logs'))

# Results
up_signal_up_win = 0      # UP 90-98c in last 2 min, UP won
up_signal_down_win = 0    # UP 90-98c in last 2 min, DOWN won (REVERSAL)
down_signal_down_win = 0  # DOWN 90-98c in last 2 min, DOWN won
down_signal_up_win = 0    # DOWN 90-98c in last 2 min, UP won (REVERSAL)

no_signal_last_2min = 0   # No 90-98c in last 2 min
early_settle = 0          # Hit 99c/100c before last 2 min

total_windows = 0

for folder in folders:
    file = f'backtesting15mbitcoin/market_logs/{folder}/{folder}.txt'
    
    if not os.path.exists(file):
        continue
    
    # Parse folder name for end time
    parts = folder.split('_')
    if len(parts) < 7:
        continue
    
    try:
        end_min = int(parts[6])
    except:
        continue
    
    # Read and parse ticks
    lines = open(file).readlines()
    
    ticks = []
    for line in lines:
        match = re.match(r'(\d+):(\d+):\d+ - UP (\d+)C \| DOWN (\d+)C', line.strip())
        if match:
            mins, secs, up, down = match.groups()
            tick_secs = int(mins) * 60 + int(secs)
            secs_left = end_min * 60 - tick_secs
            ticks.append({
                'secs_left': secs_left,
                'up': int(up),
                'down': int(down)
            })
    
    if not ticks:
        continue
    
    total_windows += 1
    
    # Determine winner
    max_up = max(t['up'] for t in ticks)
    max_down = max(t['down'] for t in ticks)
    winner = 'UP' if max_up > max_down else 'DOWN'
    
    # Check if settled early (99c/100c before last 2 min)
    settled_early = False
    for t in ticks:
        if t['secs_left'] > 120:  # Before last 2 min
            if t['up'] >= 99 or t['down'] >= 99:
                settled_early = True
                break
    
    if settled_early:
        early_settle += 1
        continue  # Skip - already decided before our window
    
    # Check for 90-98c in last 2 min
    last_2min = [t for t in ticks if 0 <= t['secs_left'] <= 120]
    
    up_90_98 = any(90 <= t['up'] <= 98 for t in last_2min)
    down_90_98 = any(90 <= t['down'] <= 98 for t in last_2min)
    
    if not up_90_98 and not down_90_98:
        no_signal_last_2min += 1
        continue
    
    # Count results
    if up_90_98 and winner == 'UP':
        up_signal_up_win += 1
    elif up_90_98 and winner == 'DOWN':
        up_signal_down_win += 1
    elif down_90_98 and winner == 'DOWN':
        down_signal_down_win += 1
    elif down_90_98 and winner == 'UP':
        down_signal_up_win += 1

# Print results
print("=" * 60)
print("BACKTEST RESULTS - 90-98c IN LAST 2 MINUTES")
print("=" * 60)
print()
print(f"Total windows analyzed: {total_windows}")
print(f"Settled early (99c before last 2 min): {early_settle}")
print(f"No 90-98c signal in last 2 min: {no_signal_last_2min}")
print()
print("-" * 60)
print("TRADE SIGNALS (90-98c in last 2 min)")
print("-" * 60)
print()
print(f"UP signal, UP won (WIN):    {up_signal_up_win}")
print(f"UP signal, DOWN won (LOSS): {up_signal_down_win}")
print(f"DOWN signal, DOWN won (WIN):  {down_signal_down_win}")
print(f"DOWN signal, UP won (LOSS):   {down_signal_up_win}")
print()

total_trades = up_signal_up_win + up_signal_down_win + down_signal_down_win + down_signal_up_win
total_wins = up_signal_up_win + down_signal_down_win
total_losses = up_signal_down_win + down_signal_up_win

print("-" * 60)
print("SUMMARY")
print("-" * 60)
print(f"Total trades: {total_trades}")
print(f"Wins: {total_wins}")
print(f"Losses (reversals): {total_losses}")
if total_trades > 0:
    print(f"Win rate: {total_wins/total_trades*100:.2f}%")
print("=" * 60)

