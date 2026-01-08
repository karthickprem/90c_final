"""
Test multiple position sizes to find optimal configuration
"""

import os
import re
import json

ENTRY_MIN = 0.90
ENTRY_MAX = 0.99
STARTING_BALANCE = 10.0
DATA_DIR = "backtesting15mbitcoin/market_logs"


def parse_tick_line(line):
    try:
        match = re.match(r'(\d+):(\d+):\d+ - UP (\d+)C \| DOWN (\d+)C', line.strip())
        if match:
            mins, secs, up, down = match.groups()
            return int(mins), int(secs), int(up) / 100.0, int(down) / 100.0
    except:
        pass
    return None


def load_window(folder_path):
    folder_name = os.path.basename(folder_path)
    file_path = os.path.join(folder_path, folder_name + ".txt")
    
    if not os.path.exists(file_path):
        return None
    
    ticks = []
    
    try:
        with open(file_path, 'r') as f:
            for line in f:
                tick = parse_tick_line(line)
                if tick:
                    mins, secs, up, down = tick
                    total_secs = mins * 60 + secs
                    secs_left = 900 - total_secs
                    
                    ticks.append({
                        'secs_left': secs_left,
                        'up': up,
                        'down': down
                    })
        
        if not ticks or len(ticks) < 10:
            return None
        
        last = ticks[-1]
        if last['up'] >= 0.95:
            winner = 'up'
        elif last['down'] >= 0.95:
            winner = 'down'
        else:
            winner = None
        
        return {'folder': folder_name, 'ticks': ticks, 'winner': winner}
    except:
        return None


def test_position_size(position_pct):
    """Test strategy with specific position size"""
    folders = sorted([d for d in os.listdir(DATA_DIR) 
                     if os.path.isdir(os.path.join(DATA_DIR, d))])
    
    balance = STARTING_BALANCE
    wins = 0
    losses = 0
    
    for folder in folders:
        window = load_window(os.path.join(DATA_DIR, folder))
        
        if not window or not window['winner']:
            continue
        
        # Find first 90-99c
        entry = None
        for tick in window['ticks']:
            if ENTRY_MIN <= tick['up'] <= ENTRY_MAX:
                entry = ('up', tick['up'])
                break
            if ENTRY_MIN <= tick['down'] <= ENTRY_MAX:
                entry = ('down', tick['down'])
                break
        
        if not entry:
            continue
        
        side, price = entry
        use = balance * position_pct
        shares = use / price
        
        if shares < 5:
            continue
        
        won = (side == window['winner'])
        
        if won:
            payout = shares
            profit = payout - use
            balance += profit
            wins += 1
        else:
            balance -= use
            losses += 1
    
    total = wins + losses
    win_rate = (wins / total * 100) if total > 0 else 0
    pnl = balance - STARTING_BALANCE
    roi = (pnl / STARTING_BALANCE * 100)
    
    return {
        'position_pct': position_pct,
        'trades': total,
        'wins': wins,
        'losses': losses,
        'win_rate': win_rate,
        'final_balance': balance,
        'pnl': pnl,
        'roi': roi
    }


print("=" * 70)
print("POSITION SIZE SWEEP - 90-99c ENTRY")
print("=" * 70)
print()

# Test different position sizes
sizes = [0.10, 0.15, 0.20, 0.25, 0.30, 0.35, 0.40, 0.50, 0.60, 0.70]

results = []

for pct in sizes:
    result = test_position_size(pct)
    results.append(result)
    
    print(f"{int(pct*100):3}% position: {result['trades']:3} trades | "
          f"W{result['wins']}/L{result['losses']} ({result['win_rate']:.1f}%) | "
          f"${result['final_balance']:6.2f} ({result['roi']:+6.1f}%)")

# Find best
best = max(results, key=lambda x: x['roi'])

print()
print("=" * 70)
print(f"BEST RESULT: {int(best['position_pct']*100)}% position")
print(f"  Final: ${best['final_balance']:.2f}")
print(f"  ROI: {best['roi']:+.1f}%")
print(f"  Win Rate: {best['win_rate']:.1f}%")
print("=" * 70)





