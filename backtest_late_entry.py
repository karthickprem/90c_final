"""
Backtest Late-Entry Strategy on Historical Data

Uses dataset from: github.com/milanzandbak/backtesting15mbitcoin
50 days of BTC 15-min price data (Nov 1 - Dec 19)

Tests: Entry at 85-99c in last 3 minutes, hold to settlement
"""

import os
import pandas as pd
import json
from datetime import datetime

# === CONFIG ===
ENTRY_MIN = 0.85
ENTRY_MAX = 0.99
ENTRY_WINDOW_SECS = 180  # Last 3 minutes
POSITION_PCT = 0.70
STARTING_BALANCE = 10.0

# Paths (update these after download)
DATA_DIR = "backtest_data"
BUY_LOGS = os.path.join(DATA_DIR, "market_logs")  # Unzipped folder
SELL_LOGS = os.path.join(DATA_DIR, "market_logs_sell")  # Unzipped folder


def parse_filename(filename):
    """
    Extract window info from filename
    Example: btc_updown_15m_1699142400_log.csv
    Returns: start_timestamp
    """
    try:
        parts = filename.replace("_log.csv", "").split("_")
        timestamp = int(parts[-1])
        return timestamp
    except:
        return None


def load_window_data(start_ts):
    """Load buy/sell data for a specific window"""
    filename = f"btc_updown_15m_{start_ts}_log.csv"
    
    buy_file = os.path.join(BUY_LOGS, filename)
    sell_file = os.path.join(SELL_LOGS, filename)
    
    if not os.path.exists(buy_file):
        return None
    
    try:
        # Load buy prices (what we can buy at)
        buy_df = pd.read_csv(buy_file)
        
        # Expected columns: timestamp, up_price, down_price
        # Calculate time remaining for each tick
        buy_df['secs_left'] = start_ts + 900 - buy_df['timestamp']
        
        return buy_df
    except Exception as e:
        print(f"Error loading {filename}: {e}")
        return None


def find_entry_in_window(df):
    """
    Find if entry signal appeared in last 3 minutes
    
    Returns: (side, price, secs_left) or None
    """
    if df is None or df.empty:
        return None
    
    # Filter to entry window (30s to 180s remaining)
    entry_window = df[(df['secs_left'] >= 30) & (df['secs_left'] <= ENTRY_WINDOW_SECS)]
    
    if entry_window.empty:
        return None
    
    # Check UP side
    up_entries = entry_window[
        (entry_window['up_price'] >= ENTRY_MIN) & 
        (entry_window['up_price'] <= ENTRY_MAX)
    ]
    
    if not up_entries.empty:
        first_up = up_entries.iloc[0]
        return ('up', first_up['up_price'], first_up['secs_left'])
    
    # Check DOWN side  
    down_entries = entry_window[
        (entry_window['down_price'] >= ENTRY_MIN) & 
        (entry_window['down_price'] <= ENTRY_MAX)
    ]
    
    if not down_entries.empty:
        first_down = down_entries.iloc[0]
        return ('down', first_down['down_price'], first_down['secs_left'])
    
    return None


def determine_winner(df):
    """
    Determine winner from final prices
    Last tick should show ~100c for winner, ~0c for loser
    """
    if df is None or df.empty:
        return None
    
    # Get last tick (at window close)
    last_tick = df.iloc[-1]
    
    up_final = last_tick['up_price']
    down_final = last_tick['down_price']
    
    if up_final >= 0.95:
        return 'up'
    elif down_final >= 0.95:
        return 'down'
    else:
        return None  # Unclear


def run_backtest():
    """Run backtest on all available windows"""
    
    print("=" * 60)
    print("BACKTESTING LATE-ENTRY STRATEGY")
    print("=" * 60)
    print(f"Entry: {ENTRY_MIN*100:.0f}c-{ENTRY_MAX*100:.0f}c")
    print(f"Window: Last {ENTRY_WINDOW_SECS}s to 30s")
    print(f"Position: {POSITION_PCT*100:.0f}%")
    print("=" * 60)
    
    if not os.path.exists(BUY_LOGS):
        print(f"\nERROR: Data directory not found: {BUY_LOGS}")
        print("\nPLEASE:")
        print("1. Download from: github.com/milanzandbak/backtesting15mbitcoin")
        print("2. Unzip market_logs.zip")
        print("3. Place in: backtest_data/market_logs/")
        return
    
    # Get all window files
    files = [f for f in os.listdir(BUY_LOGS) if f.endswith("_log.csv")]
    print(f"\nFound {len(files)} historical windows")
    
    balance = STARTING_BALANCE
    trades = []
    wins = 0
    losses = 0
    skips = 0
    
    for i, filename in enumerate(sorted(files)):
        start_ts = parse_filename(filename)
        if not start_ts:
            continue
        
        # Load window data
        df = load_window_data(start_ts)
        if df is None:
            continue
        
        # Find entry opportunity
        entry = find_entry_in_window(df)
        
        if not entry:
            skips += 1
            continue  # No entry signal in this window
        
        side, price, secs_left = entry
        
        # Simulate trade
        use = balance * POSITION_PCT
        shares = use / price
        
        if shares < 5:  # Min order size
            skips += 1
            continue
        
        # Determine winner
        winner = determine_winner(df)
        
        if winner is None:
            skips += 1
            continue  # Unclear outcome
        
        won = (winner == side)
        
        if won:
            payout = shares * 1.0
            profit = payout - use
            balance += profit
            wins += 1
            result = "WIN"
        else:
            balance -= use
            losses += 1
            result = "LOSS"
        
        trades.append({
            "window": filename,
            "side": side,
            "price": price,
            "entry_time": secs_left,
            "cost": use,
            "shares": shares,
            "winner": winner,
            "result": result,
            "pnl": profit if won else -use,
            "balance": balance
        })
        
        # Progress
        if (i+1) % 10 == 0:
            print(f"  Processed {i+1}/{len(files)} windows | Trades: {len(trades)} | Balance: ${balance:.2f}")
    
    # Calculate stats
    total_trades = wins + losses
    win_rate = (wins / total_trades * 100) if total_trades > 0 else 0
    pnl = balance - STARTING_BALANCE
    roi = (pnl / STARTING_BALANCE * 100) if STARTING_BALANCE > 0 else 0
    
    # Print results
    print("\n" + "=" * 60)
    print("BACKTEST RESULTS")
    print("=" * 60)
    print(f"Windows processed: {len(files)}")
    print(f"Entry opportunities: {len(trades)}")
    print(f"Skipped (no signal): {skips}")
    print()
    print(f"Starting balance: ${STARTING_BALANCE:.2f}")
    print(f"Final balance:    ${balance:.2f}")
    print(f"Total P&L:        ${pnl:+.2f}")
    print(f"ROI:              {roi:+.1f}%")
    print()
    print(f"Trades: {total_trades}")
    print(f"Wins:   {wins}")
    print(f"Losses: {losses}")
    print(f"Win Rate: {win_rate:.1f}%")
    print("=" * 60)
    
    # Save results
    results = {
        "config": {
            "entry_min": ENTRY_MIN,
            "entry_max": ENTRY_MAX,
            "entry_window_secs": ENTRY_WINDOW_SECS,
            "position_pct": POSITION_PCT
        },
        "summary": {
            "windows_processed": len(files),
            "trades": total_trades,
            "wins": wins,
            "losses": losses,
            "win_rate": win_rate,
            "starting_balance": STARTING_BALANCE,
            "final_balance": balance,
            "pnl": pnl,
            "roi": roi
        },
        "trades": trades
    }
    
    output_file = f"backtest_results_{datetime.now().strftime('%Y%m%d_%H%M%S')}.json"
    with open(output_file, 'w') as f:
        json.dump(results, f, indent=2)
    
    print(f"\nResults saved to: {output_file}")
    
    return results


if __name__ == "__main__":
    print("\nBACKTEST SETUP:")
    print("1. Download: https://github.com/milanzandbak/backtesting15mbitcoin")
    print("2. Unzip market_logs.zip to backtest_data/market_logs/")
    print("3. Run this script")
    print("\nStarting backtest...\n")
    
    run_backtest()

