"""Find a window with 90-98c in last 2 min"""

import os
import re

folders = sorted(os.listdir('backtesting15mbitcoin/market_logs'))[:500]

found = []

for folder in folders:
    file = f'backtesting15mbitcoin/market_logs/{folder}/{folder}.txt'
    
    if not os.path.exists(file):
        continue
    
    # Get end time
    parts = folder.split('_')
    if len(parts) < 7:
        continue
    
    end_min = int(parts[6])
    
    # Read lines
    lines = open(file).readlines()
    
    # Check last 2 min for 90-98c
    for line in lines:
        match = re.match(r'(\d+):(\d+):\d+ - UP (\d+)C \| DOWN (\d+)C', line.strip())
        if match:
            mins, secs, up, down = match.groups()
            tick_secs = int(mins) * 60 + int(secs)
            secs_left = end_min * 60 - tick_secs
            
            if 0 <= secs_left <= 120:  # Last 2 min
                up_val = int(up)
                down_val = int(down)
                
                if 90 <= up_val <= 98 or 90 <= down_val <= 98:
                    found.append(folder)
                    break
    
    if len(found) >= 5:
        break

print("Windows with 90-98c in last 2 min:")
for f in found:
    print(f"  {f}")





