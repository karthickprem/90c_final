"""
Simple Backtest: Enter at 90-99c ANYTIME (no timer constraint)

Tests the core hypothesis: Can we profit buying at 90-99c?
"""

import os
import re
import json
from datetime import datetime

# === CONFIG ===
ENTRY_MIN = 0.90
ENTRY_MAX = 0.99
POSITION_PCT = 0.30  # REDUCED from 70% to 30%
STARTING_BALANCE = 10.0

DATA_DIR = "backtesting15mbitcoin/market_logs"


def parse_tick_line(line):
    """Parse: '45:00:177 - UP 55C | DOWN 49C'"""
    try:
        match = re.match(r'(\d+):(\d+):\d+ - UP (\d+)C \| DOWN (\d+)C', line.strip())
        if match:
            mins, secs, up, down = match.groups()
            return int(mins), int(secs), int(up) / 100.0, int(down) / 100.0
    except:
        pass
    return None


def load_window(folder_path):
    """Load window tick data"""
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
        
        # Winner from last tick
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
        
    except:
        return None


def find_entry(window_data):
    """Find FIRST time 90-99c appears (ANYTIME in window)"""
    if not window_data:
        return None
    
    for tick in window_data['ticks']:
        # NO TIME CONSTRAINT - just check price
        up = tick['up']
        down = tick['down']
        secs_left = tick['secs_left']
        
        # Check UP
        if ENTRY_MIN <= up <= ENTRY_MAX:
            return {'side': 'up', 'price': up, 'time': secs_left}
        
        # Check DOWN
        if ENTRY_MIN <= down <= ENTRY_MAX:
            return {'side': 'down', 'price': down, 'time': secs_left}
    
    return None


def run_backtest():
    print("=" * 70)
    print("SIMPLE BACKTEST: 90-99c ENTRY (NO TIME CONSTRAINT)")
    print("=" * 70)
    print(f"Entry range: {ENTRY_MIN*100:.0f}c-{ENTRY_MAX*100:.0f}c")
    print(f"Entry timing: ANYTIME (no timer constraint)")
    print(f"Position size: {POSITION_PCT*100:.0f}%")
    print(f"Starting balance: ${STARTING_BALANCE:.2f}")
    print("=" * 70)
    
    if not os.path.exists(DATA_DIR):
        print(f"\nERROR: {DATA_DIR} not found!")
        print("Please ensure backtesting15mbitcoin/ is in the current directory")
        return
    
    folders = sorted([d for d in os.listdir(DATA_DIR) 
                     if os.path.isdir(os.path.join(DATA_DIR, d))])
    
    print(f"\nLoaded {len(folders)} windows from dataset")
    print("Processing...\n")
    
    balance = STARTING_BALANCE
    trades = []
    wins = 0
    losses = 0
    no_signal = 0
    no_outcome = 0
    
    for i, folder in enumerate(folders):
        folder_path = os.path.join(DATA_DIR, folder)
        window = load_window(folder_path)
        
        if not window:
            continue
        
        if not window['winner']:
            no_outcome += 1
            continue
        
        # Find entry
        entry = find_entry(window)
        if not entry:
            no_signal += 1
            continue
        
        # Simulate trade
        use = balance * POSITION_PCT
        shares = use / entry['price']
        
        if shares < 5:
            no_signal += 1
            continue
        
        # Outcome
        won = (entry['side'] == window['winner'])
        
        if won:
            payout = shares * 1.0
            profit = payout - use
            balance += profit
            wins += 1
        else:
            loss = use
            balance -= loss
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
            'pnl': profit if won else -loss,
            'balance': balance
        })
        
        # Progress
        if (i+1) % 1000 == 0:
            total_so_far = wins + losses
            wr_so_far = (wins/total_so_far*100) if total_so_far > 0 else 0
            print(f"  {i+1}/{len(folders)} processed | Trades: {total_so_far} | W{wins}/L{losses} ({wr_so_far:.1f}%) | ${balance:.2f}")
    
    # Final stats
    total_trades = wins + losses
    win_rate = (wins / total_trades * 100) if total_trades > 0 else 0
    pnl = balance - STARTING_BALANCE
    roi = (pnl / STARTING_BALANCE * 100)
    
    print("\n" + "=" * 70)
    print("BACKTEST RESULTS")
    print("=" * 70)
    print(f"Total windows: {len(folders)}")
    print(f"Skipped (no 90-99c): {no_signal}")
    print(f"Skipped (unclear outcome): {no_outcome}")
    print(f"Traded: {total_trades}")
    print()
    print(f"Starting balance: ${STARTING_BALANCE:.2f}")
    print(f"Final balance:    ${balance:.2f}")
    print(f"P&L:              ${pnl:+.2f}")
    print(f"ROI:              {roi:+.1f}%")
    print()
    print(f"Wins:     {wins}")
    print(f"Losses:   {losses}")
    print(f"Win Rate: {win_rate:.1f}%")
    print("=" * 70)
    
    # Show first 10 trades
    print("\nFirst 10 Trades:")
    for t in trades[:10]:
        result = "WIN" if t['won'] else "LOSS"
        print(f"  {t['side'].upper()} @ {t['price']*100:.0f}c [T-{int(t['entry_time'])}s] -> {result:4} | Balance: ${t['balance']:.2f}")
    
    # Entry time distribution
    if trades:
        print("\nEntry Time Distribution:")
        early = len([t for t in trades if t['entry_time'] > 180])
        mid = len([t for t in trades if 60 < t['entry_time'] <= 180])
        late = len([t for t in trades if t['entry_time'] <= 60])
        print(f"  Early (>3min): {early}")
        print(f"  Mid (1-3min):  {mid}")
        print(f"  Late (<1min):  {late}")
    
    # Save
    output = {
        "config": {
            "entry_min": ENTRY_MIN,
            "entry_max": ENTRY_MAX,
            "entry_window": "anytime",
            "position_pct": POSITION_PCT
        },
        "results": {
            "windows": len(folders),
            "trades": total_trades,
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
    
    filename = f"backtest_simple_{datetime.now().strftime('%Y%m%d_%H%M%S')}.json"
    with open(filename, 'w') as f:
        json.dump(output, f, indent=2)
    
    print(f"\nResults saved to: {filename}")
    
    # Decision
    print("\n" + "=" * 70)
    if win_rate >= 90 and roi > 10:
        print("VERDICT: PROFITABLE!")
        print(f"Win rate {win_rate:.1f}% with {roi:+.1f}% ROI")
    elif roi > 0:
        print("VERDICT: MARGINALLY PROFITABLE")
        print(f"Positive ROI but low: {roi:+.1f}%")
    else:
        print("VERDICT: NOT PROFITABLE")
        print(f"Negative ROI: {roi:.1f}% despite {win_rate:.1f}% win rate")
        print("\nREASON: Position size too large for this win rate")
        print("SOLUTION: Reduce POSITION_PCT to 20-30%")
    print("=" * 70)


if __name__ == "__main__":
    run_backtest()

