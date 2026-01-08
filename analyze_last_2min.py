"""
Analyze LAST 2 MINUTES ONLY

Key filters:
1. Only look at last 120 seconds (2:00 to 0:00)
2. Check if 90-99c appears in this window
3. If price hit 100c BEFORE last 2 min, skip window (already decided)
4. Calculate reversal rate for late entries
"""

import os
import re

DATA_DIR = "backtesting15mbitcoin/market_logs"


def parse_tick_line(line):
    try:
        match = re.match(r'(\d+):(\d+):\d+ - UP (\d+)C \| DOWN (\d+)C', line.strip())
        if match:
            mins, secs, up, down = match.groups()
            total_secs = int(mins) * 60 + int(secs)
            secs_left = 900 - total_secs
            return {
                'secs_left': secs_left,
                'up': int(up) / 100.0,
                'down': int(down) / 100.0
            }
    except:
        pass
    return None


def analyze_window_last_2min(folder_path):
    """
    Analyze if 90-99c appears in LAST 2 MINUTES only
    Skip if either side hit 100c before last 2 min
    """
    folder_name = os.path.basename(folder_path)
    file_path = os.path.join(folder_path, folder_name + ".txt")
    
    if not os.path.exists(file_path):
        return None
    
    try:
        with open(file_path, 'r') as f:
            lines = f.readlines()
        
        ticks = []
        for line in lines:
            tick = parse_tick_line(line)
            if tick:
                ticks.append(tick)
        
        if len(ticks) < 10:
            return None
        
        # Step 1: Check if 100c hit BEFORE last 2 min
        early_ticks = [t for t in ticks if t['secs_left'] > 120]
        if early_ticks:
            max_up_early = max(t['up'] for t in early_ticks)
            max_down_early = max(t['down'] for t in early_ticks)
            
            if max_up_early >= 1.00 or max_down_early >= 1.00:
                return {'skip_reason': 'already_decided'}  # Window decided early
        
        # Step 2: Look at LAST 2 MINUTES only (120s to 0s)
        last_2min_ticks = [t for t in ticks if 0 <= t['secs_left'] <= 120]
        
        if not last_2min_ticks:
            return {'skip_reason': 'no_late_data'}
        
        # Step 3: Check if 90-99c appears in last 2 min
        up_90_99_in_last_2min = False
        down_90_99_in_last_2min = False
        
        for tick in last_2min_ticks:
            if 0.90 <= tick['up'] <= 0.99:
                up_90_99_in_last_2min = True
            if 0.90 <= tick['down'] <= 0.99:
                down_90_99_in_last_2min = True
        
        # Step 4: Determine winner
        max_up = max(t['up'] for t in ticks)
        max_down = max(t['down'] for t in ticks)
        
        if max_up >= 0.95 and max_up > max_down:
            winner = 'up'
        elif max_down >= 0.95 and max_down > max_up:
            winner = 'down'
        else:
            winner = None
        
        return {
            'folder': folder_name,
            'up_90_99_last_2min': up_90_99_in_last_2min,
            'down_90_99_last_2min': down_90_99_in_last_2min,
            'winner': winner,
            'max_up': max_up,
            'max_down': max_down
        }
        
    except:
        return None


def run_analysis():
    print("=" * 70)
    print("LAST 2 MINUTES ANALYSIS")
    print("=" * 70)
    print("Question: How often does 90-99c in LAST 2 MIN reverse?")
    print("Filter: Skip windows where 100c hit before last 2 min")
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
    total_analyzed = 0
    skipped_already_decided = 0
    skipped_no_data = 0
    unclear_winner = 0
    
    # Last 2 min stats
    up_90_99_last_2min = 0
    down_90_99_last_2min = 0
    both_90_99_last_2min = 0
    
    # Reversals in last 2 min
    up_90_99_but_down_won = 0
    down_90_99_but_up_won = 0
    
    # Safe entries in last 2 min
    up_90_99_and_up_won = 0
    down_90_99_and_down_won = 0
    
    reversal_examples = []
    
    for i, folder in enumerate(folders):
        result = analyze_window_last_2min(os.path.join(DATA_DIR, folder))
        
        if not result:
            continue
        
        if 'skip_reason' in result:
            if result['skip_reason'] == 'already_decided':
                skipped_already_decided += 1
            else:
                skipped_no_data += 1
            continue
        
        if not result['winner']:
            unclear_winner += 1
            continue
        
        total_analyzed += 1
        winner = result['winner']
        
        # Track touches in last 2 min
        if result['up_90_99_last_2min']:
            up_90_99_last_2min += 1
            
            if winner == 'up':
                up_90_99_and_up_won += 1
            else:  # DOWN won - REVERSAL!
                up_90_99_but_down_won += 1
                if len(reversal_examples) < 10:
                    reversal_examples.append({
                        'window': folder,
                        'type': 'UP touched, DOWN won',
                        'max_up': result['max_up'],
                        'max_down': result['max_down']
                    })
        
        if result['down_90_99_last_2min']:
            down_90_99_last_2min += 1
            
            if winner == 'down':
                down_90_99_and_down_won += 1
            else:  # UP won - REVERSAL!
                down_90_99_but_up_won += 1
                if len(reversal_examples) < 10:
                    reversal_examples.append({
                        'window': folder,
                        'type': 'DOWN touched, UP won',
                        'max_up': result['max_up'],
                        'max_down': result['max_down']
                    })
        
        if result['up_90_99_last_2min'] and result['down_90_99_last_2min']:
            both_90_99_last_2min += 1
        
        # Progress
        if (i+1) % 1000 == 0:
            print(f"  {i+1}/{len(folders)} processed...")
    
    # Results
    print()
    print("=" * 70)
    print("RESULTS - LAST 2 MINUTES ONLY")
    print("=" * 70)
    print(f"Total windows: {len(folders)}")
    print(f"Skipped (100c hit early): {skipped_already_decided}")
    print(f"Skipped (no data): {skipped_no_data}")
    print(f"Unclear winner: {unclear_winner}")
    print(f"Analyzed: {total_analyzed}")
    print()
    
    print("90-99c IN LAST 2 MINUTES:")
    print(f"  UP 90-99c:   {up_90_99_last_2min:4} windows ({up_90_99_last_2min/total_analyzed*100:.1f}%)")
    print(f"  DOWN 90-99c: {down_90_99_last_2min:4} windows ({down_90_99_last_2min/total_analyzed*100:.1f}%)")
    print(f"  BOTH 90-99c: {both_90_99_last_2min:4} windows ({both_90_99_last_2min/total_analyzed*100:.1f}%)")
    print()
    
    print("=" * 70)
    print("REVERSALS IN LAST 2 MIN")
    print("=" * 70)
    if up_90_99_last_2min > 0:
        print(f"UP 90-99c in last 2min but DOWN won:  {up_90_99_but_down_won:4} ({up_90_99_but_down_won/up_90_99_last_2min*100:.1f}% reversal rate)")
    else:
        print(f"UP 90-99c in last 2min but DOWN won:  0 (no UP data)")
    
    if down_90_99_last_2min > 0:
        print(f"DOWN 90-99c in last 2min but UP won:  {down_90_99_but_up_won:4} ({down_90_99_but_up_won/down_90_99_last_2min*100:.1f}% reversal rate)")
    else:
        print(f"DOWN 90-99c in last 2min but UP won:  0 (no DOWN data)")
    
    print()
    
    total_late_touches = up_90_99_last_2min + down_90_99_last_2min
    total_late_reversals = up_90_99_but_down_won + down_90_99_but_up_won
    
    if total_late_touches > 0:
        late_reversal_rate = total_late_reversals / total_late_touches * 100
        print(f"TOTAL LATE REVERSALS: {total_late_reversals}/{total_late_touches} ({late_reversal_rate:.1f}%)")
        print()
    
    print("=" * 70)
    print("SAFE ENTRIES IN LAST 2 MIN")
    print("=" * 70)
    if up_90_99_last_2min > 0:
        print(f"UP 90-99c in last 2min and UP won:    {up_90_99_and_up_won:4} ({up_90_99_and_up_won/up_90_99_last_2min*100:.1f}% success rate)")
    if down_90_99_last_2min > 0:
        print(f"DOWN 90-99c in last 2min and DOWN won: {down_90_99_and_down_won:4} ({down_90_99_and_down_won/down_90_99_last_2min*100:.1f}% success rate)")
    print()
    
    if reversal_examples:
        print("=" * 70)
        print("EXAMPLE REVERSALS IN LAST 2 MIN:")
        print("=" * 70)
        for ex in reversal_examples[:10]:
            print(f"  {ex['window']}: {ex['type']}")
            print(f"    UP max: {ex['max_up']*100:.0f}c, DOWN max: {ex['max_down']*100:.0f}c")
    
    print()
    print("=" * 70)
    print("CONCLUSION:")
    if total_late_touches > 0:
        win_rate = ((total_late_touches - total_late_reversals) / total_late_touches * 100)
        print(f"Win rate for last 2min entries: {win_rate:.1f}%")
        
        if win_rate >= 95:
            print("EXCELLENT! Very safe to enter in last 2 min")
        elif win_rate >= 90:
            print("GOOD! Late entry reduces reversal risk")
        elif win_rate >= 85:
            print("MODERATE: Still has reversal risk")
        else:
            print("RISKY: High reversal rate even in last 2 min")
    print("=" * 70)


if __name__ == "__main__":
    run_analysis()





