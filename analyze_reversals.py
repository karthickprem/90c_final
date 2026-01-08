"""
Analyze Reversals: When 90c touches on one side but OTHER side wins

This shows the RISK of entering at 90c
"""

import os
import re

DATA_DIR = "backtesting15mbitcoin/market_logs"


# Removed - parsing now done inline in analyze_window


def analyze_window(folder_path, last_n_seconds=None):
    """
    Check:
    1. Did UP touch 90c+ (optionally in last N seconds)?
    2. Did DOWN touch 90c+ (optionally in last N seconds)?
    3. Which side won?
    """
    folder_name = os.path.basename(folder_path)
    file_path = os.path.join(folder_path, folder_name + ".txt")
    
    if not os.path.exists(file_path):
        return None
    
    up_touched_90 = False
    down_touched_90 = False
    max_up = 0
    max_down = 0
    
    try:
        with open(file_path, 'r') as f:
            for line in f:
                # Parse line to get time AND prices
                match = re.match(r'(\d+):(\d+):\d+ - UP (\d+)C \| DOWN (\d+)C', line.strip())
                if not match:
                    continue
                
                mins, secs, up_val, down_val = match.groups()
                
                # Extract window end time from folder name
                # Format: 25_10_31_HH_MM_HH_MM (start hour_min, end hour_min)
                parts = folder_name.split('_')
                if len(parts) >= 7:
                    end_min = int(parts[5])  # End minute
                    
                    # Calculate time left
                    current_time_secs = int(mins) * 60 + int(secs)
                    end_time_secs = end_min * 60
                    secs_left = end_time_secs - current_time_secs
                else:
                    # Fallback: assume 15-min window
                    secs_left = 900 - (int(mins) * 60 + int(secs))
                
                up = int(up_val) / 100.0
                down = int(down_val) / 100.0
                
                # If filtering by time, only check within that window
                if last_n_seconds is None or secs_left <= last_n_seconds:
                    # Track if 90-98c touched IN THE TIME WINDOW (NOT 99c or 100c!)
                    if 0.90 <= up <= 0.98:
                        up_touched_90 = True
                    if 0.90 <= down <= 0.98:
                        down_touched_90 = True
                
                # Track max prices OVERALL (for winner determination)
                if up > max_up:
                    max_up = up
                if down > max_down:
                    max_down = down
        
        # Determine winner
        if max_up >= 0.95 and max_up > max_down:
            winner = 'up'
        elif max_down >= 0.95 and max_down > max_up:
            winner = 'down'
        else:
            winner = None
        
        return {
            'folder': folder_name,
            'up_touched_90': up_touched_90,
            'down_touched_90': down_touched_90,
            'winner': winner,
            'max_up': max_up,
            'max_down': max_down
        }
        
    except:
        return None


def run_analysis(last_n_seconds=None):
    """
    Run analysis
    last_n_seconds: If set, only check touches within last N seconds
                    If None, check entire window
    """
    time_desc = f"LAST {last_n_seconds}s" if last_n_seconds else "ENTIRE WINDOW"
    
    print("=" * 70)
    print(f"REVERSAL ANALYSIS - {time_desc}")
    print("=" * 70)
    print(f"Range: 90c-98c ONLY (excludes 99c and 100c)")
    print(f"Question: How often does 90-98c in {time_desc.lower()} reverse?")
    print("=" * 70)
    print()
    
    if not os.path.exists(DATA_DIR):
        print(f"ERROR: {DATA_DIR} not found!")
        return
    
    folders = sorted([d for d in os.listdir(DATA_DIR) 
                     if os.path.isdir(os.path.join(DATA_DIR, d))])
    
    print(f"Analyzing {len(folders)} windows...")
    print()
    
    # Counters
    total_windows = 0
    unclear_winner = 0
    
    up_touched_90 = 0
    down_touched_90 = 0
    both_touched_90 = 0
    
    # Reversals (the key metric!)
    up_touched_but_down_won = 0
    down_touched_but_up_won = 0
    
    # No reversals (safe entries)
    up_touched_and_up_won = 0
    down_touched_and_down_won = 0
    
    examples_up_reversal = []
    examples_down_reversal = []
    
    for i, folder in enumerate(folders):
        window = analyze_window(os.path.join(DATA_DIR, folder), last_n_seconds)
        
        if not window:
            continue
        
        total_windows += 1
        
        if not window['winner']:
            unclear_winner += 1
            continue
        
        winner = window['winner']
        
        # Track touches
        if window['up_touched_90']:
            up_touched_90 += 1
            
            if winner == 'up':
                up_touched_and_up_won += 1
            else:  # winner == 'down'
                up_touched_but_down_won += 1
                if len(examples_up_reversal) < 5:
                    examples_up_reversal.append({
                        'window': folder,
                        'max_up': window['max_up'],
                        'max_down': window['max_down']
                    })
        
        if window['down_touched_90']:
            down_touched_90 += 1
            
            if winner == 'down':
                down_touched_and_down_won += 1
            else:  # winner == 'up'
                down_touched_but_up_won += 1
                if len(examples_down_reversal) < 5:
                    examples_down_reversal.append({
                        'window': folder,
                        'max_up': window['max_up'],
                        'max_down': window['max_down']
                    })
        
        if window['up_touched_90'] and window['down_touched_90']:
            both_touched_90 += 1
        
        # Progress
        if (i+1) % 1000 == 0:
            print(f"  Processed {i+1}/{len(folders)}...")
    
    # Results
    print()
    print("=" * 70)
    print("RESULTS")
    print("=" * 70)
    print(f"Total windows analyzed: {total_windows}")
    print(f"Unclear winners: {unclear_winner}")
    print()
    
    print("90-98c TOUCH FREQUENCY:")
    print(f"  UP 90-98c:   {up_touched_90:4} windows ({up_touched_90/total_windows*100:.1f}%)")
    print(f"  DOWN 90-98c: {down_touched_90:4} windows ({down_touched_90/total_windows*100:.1f}%)")
    print(f"  BOTH 90-98c: {both_touched_90:4} windows ({both_touched_90/total_windows*100:.1f}%)")
    print()
    
    print("=" * 70)
    print("REVERSALS (90-98c touched but other side won)")
    print("=" * 70)
    print(f"UP 90-98c but DOWN won:   {up_touched_but_down_won:4} ({up_touched_but_down_won/up_touched_90*100:.1f}% of UP touches)")
    print(f"DOWN 90-98c but UP won:   {down_touched_but_up_won:4} ({down_touched_but_up_won/down_touched_90*100:.1f}% of DOWN touches)")
    print()
    
    total_reversals = up_touched_but_down_won + down_touched_but_up_won
    total_90c_touches = up_touched_90 + down_touched_90
    
    print(f"TOTAL REVERSALS: {total_reversals}/{total_90c_touches} ({total_reversals/total_90c_touches*100:.1f}%)")
    print()
    
    print("=" * 70)
    print("SAFE ENTRIES (90-98c and same side won)")
    print("=" * 70)
    print(f"UP 90-98c and UP won:     {up_touched_and_up_won:4} ({up_touched_and_up_won/up_touched_90*100:.1f}% success rate)")
    print(f"DOWN 90-98c and DOWN won: {down_touched_and_down_won:4} ({down_touched_and_down_won/down_touched_90*100:.1f}% success rate)")
    print()
    
    if examples_up_reversal:
        print("=" * 70)
        print("EXAMPLE REVERSALS (UP 90-98c but DOWN won):")
        print("=" * 70)
        for ex in examples_up_reversal:
            print(f"  {ex['window']}: UP peaked {ex['max_up']*100:.0f}c, DOWN peaked {ex['max_down']*100:.0f}c")
    
    if examples_down_reversal:
        print()
        print("=" * 70)
        print("EXAMPLE REVERSALS (DOWN 90-98c but UP won):")
        print("=" * 70)
        for ex in examples_down_reversal:
            print(f"  {ex['window']}: DOWN peaked {ex['max_down']*100:.0f}c, UP peaked {ex['max_up']*100:.0f}c")
    
    print()
    print("=" * 70)
    print("CONCLUSION:")
    reversal_rate = total_reversals / total_90c_touches * 100
    win_rate = ((total_90c_touches - total_reversals) / total_90c_touches * 100)
    
    print(f"Entry range: 90c-98c (excludes 99c, 100c)")
    print(f"Reversal rate: {reversal_rate:.2f}%")
    print(f"WIN RATE: {win_rate:.2f}%")
    print()
    
    if win_rate >= 95:
        print("EXCELLENT! Very safe to enter")
    elif win_rate >= 90:
        print("GOOD! Profitable with proper sizing")
    elif win_rate >= 85:
        print("MODERATE: Needs conservative sizing")
    else:
        print("POOR: Not recommended")
    
    print("=" * 70)


if __name__ == "__main__":
    import sys
    
    if len(sys.argv) > 1 and sys.argv[1] == "--last-2min":
        print("Running analysis for LAST 2 MINUTES only\n")
        run_analysis(last_n_seconds=120)
    else:
        print("Running analysis for ENTIRE WINDOW\n")
        print("(Use --last-2min flag for last 2 minutes only)\n")
        run_analysis(last_n_seconds=None)

