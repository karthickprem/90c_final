"""
Backtest Late-Entry Strategy on Reddit User's 50-Day Dataset

Data format:
- Folder per window: 25_12_13_07_45_08_00 (date_time_start_end)
- File: timestamp - UP XXC | DOWN YYC
- 772 ticks per window (sampled every ~1-2 seconds)
"""

import os
import re
from datetime import datetime

# === CONFIG ===
ENTRY_MIN = 0.85
ENTRY_MAX = 0.99
ENTRY_WINDOW_SECS = 180  # Last 3 minutes
POSITION_PCT = 0.70
STARTING_BALANCE = 10.0

DATA_DIR = "backtesting15mbitcoin/market_logs"


def parse_tick_line(line):
    """
    Parse: '45:00:177 - UP 55C | DOWN 49C'
    Returns: (mins, secs, up_price, down_price)
    """
    try:
        match = re.match(r'(\d+):(\d+):\d+ - UP (\d+)C \| DOWN (\d+)C', line.strip())
        if match:
            mins, secs, up, down = match.groups()
            return int(mins), int(secs), int(up) / 100.0, int(down) / 100.0
    except:
        pass
    return None


def load_window(folder_path):
    """Load and parse a window's tick data"""
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
                    secs_left = 900 - total_secs  # 15 min = 900s
                    
                    ticks.append({
                        'secs_left': secs_left,
                        'up': up,
                        'down': down
                    })
        
        if not ticks:
            return None
        
        # Determine winner from last tick
        last = ticks[-1]
        if last['up'] >= 0.95:
            winner = 'up'
        elif last['down'] >= 0.95:
            winner = 'down'
        else:
            winner = None
        
        return {
            'folder': folder_name,
            'ticks': ticks,
            'winner': winner
        }
        
    except Exception as e:
        return None


def find_entry(window_data):
    """Find first entry opportunity in the entry window"""
    if not window_data:
        return None
    
    for tick in window_data['ticks']:
        secs_left = tick['secs_left']
        
        # Check if in entry window (30s to 180s)
        if 30 <= secs_left <= ENTRY_WINDOW_SECS:
            up = tick['up']
            down = tick['down']
            
            # Check UP
            if ENTRY_MIN <= up <= ENTRY_MAX:
                return {'side': 'up', 'price': up, 'time': secs_left}
            
            # Check DOWN
            if ENTRY_MIN <= down <= ENTRY_MAX:
                return {'side': 'down', 'price': down, 'time': secs_left}
    
    return None


def run_backtest():
    print("=" * 60)
    print("BACKTESTING ON 50 DAYS OF HISTORICAL DATA")
    print("=" * 60)
    print(f"Entry: {ENTRY_MIN*100:.0f}c-{ENTRY_MAX*100:.0f}c")
    print(f"Window: Last {ENTRY_WINDOW_SECS}s to 30s")
    print(f"Position: {POSITION_PCT*100:.0f}%")
    print(f"Starting: ${STARTING_BALANCE:.2f}")
    print("=" * 60)
    
    if not os.path.exists(DATA_DIR):
        print(f"\nERROR: Data directory not found: {DATA_DIR}")
        return
    
    # Get all window folders
    folders = sorted([d for d in os.listdir(DATA_DIR) 
                     if os.path.isdir(os.path.join(DATA_DIR, d))])
    
    print(f"\nProcessing {len(folders)} windows...")
    print()
    
    balance = STARTING_BALANCE
    trades = []
    wins = 0
    losses = 0
    skips = 0
    
    for i, folder in enumerate(folders):
        folder_path = os.path.join(DATA_DIR, folder)
        
        # Load window data
        window = load_window(folder_path)
        if not window or not window['winner']:
            skips += 1
            continue
        
        # Find entry
        entry = find_entry(window)
        if not entry:
            skips += 1
            continue
        
        # Simulate trade
        use = balance * POSITION_PCT
        shares = use / entry['price']
        
        if shares < 5:  # Min order
            skips += 1
            continue
        
        # Check outcome
        won = (entry['side'] == window['winner'])
        
        if won:
            payout = shares * 1.0
            profit = payout - use
            balance += profit
            wins += 1
        else:
            balance -= use
            losses += 1
        
        trades.append({
            'window': folder,
            'side': entry['side'],
            'price': entry['price'],
            'entry_time': entry['time'],
            'winner': window['winner'],
            'won': won,
            'cost': use,
            'shares': shares,
            'pnl': profit if won else -use,
            'balance': balance
        })
        
        # Progress
        if (i+1) % 500 == 0:
            print(f"  {i+1}/{len(folders)} | Trades: {len(trades)} | W{wins}/L{losses} | ${balance:.2f}")
    
    # Results
    total = wins + losses
    win_rate = (wins / total * 100) if total > 0 else 0
    pnl = balance - STARTING_BALANCE
    roi = (pnl / STARTING_BALANCE * 100)
    
    print("\n" + "=" * 60)
    print("BACKTEST RESULTS (50 DAYS)")
    print("=" * 60)
    print(f"Windows processed: {len(folders)}")
    print(f"Trade opportunities: {len(trades)}")
    print(f"Skipped: {skips}")
    print()
    print(f"Starting: ${STARTING_BALANCE:.2f}")
    print(f"Final:    ${balance:.2f}")
    print(f"P&L:      ${pnl:+.2f}")
    print(f"ROI:      {roi:+.1f}%")
    print()
    print(f"Total Trades: {total}")
    print(f"Wins:  {wins}")
    print(f"Losses: {losses}")
    print(f"Win Rate: {win_rate:.1f}%")
    print("=" * 60)
    
    # Show some trade examples
    if trades:
        print("\nSample Trades:")
        for t in trades[:5]:
            result = "WIN" if t['won'] else "LOSS"
            print(f"  {t['window']}: {t['side'].upper()} @ {t['price']*100:.0f}c [T-{t['entry_time']}s] -> {result} ({t['winner'].upper()} won)")
    
    # Save
    import json
    output = {
        "config": {
            "entry_min": ENTRY_MIN,
            "entry_max": ENTRY_MAX,
            "entry_window_secs": ENTRY_WINDOW_SECS,
            "position_pct": POSITION_PCT
        },
        "results": {
            "windows": len(folders),
            "trades": total,
            "wins": wins,
            "losses": losses,
            "win_rate": win_rate,
            "starting": STARTING_BALANCE,
            "final": balance,
            "pnl": pnl,
            "roi": roi
        },
        "trades": trades
    }
    
    filename = f"backtest_results_{datetime.now().strftime('%Y%m%d_%H%M%S')}.json"
    with open(filename, 'w') as f:
        json.dump(output, f, indent=2)
    
    print(f"\nDetailed results saved to: {filename}")
    
    # Decision
    print("\n" + "=" * 60)
    print("DECISION:")
    if win_rate >= 90 and roi > 0:
        print("PROFITABLE! Win rate >= 90%, positive ROI")
        print("-> Proceed with LIVE trading")
    elif win_rate >= 85 and roi > 0:
        print("MARGINALLY PROFITABLE. Win rate 85-90%")
        print("-> Consider live with smaller position size")
    else:
        print("NOT PROFITABLE. Win rate < 85% or negative ROI")
        print("-> Adjust strategy or abandon")
    print("=" * 60)


if __name__ == "__main__":
    run_backtest()

