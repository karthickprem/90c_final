"""
CAREFUL ANALYSIS: Last 2 Minutes - 90-99c Reversal Rate

NO SHORTCUTS - Analyze ALL 4,872 windows properly
"""

import os
import re

DATA_DIR = "backtesting15mbitcoin/market_logs"


def parse_tick_line(line):
    """Parse tick line - CAREFUL validation"""
    try:
        match = re.match(r'(\d+):(\d+):\d+ - UP (\d+)C \| DOWN (\d+)C', line.strip())
        if match:
            mins = int(match.group(1))
            secs = int(match.group(2))
            up = int(match.group(3))
            down = int(match.group(4))
            
            # Validate ranges
            if not (0 <= mins <= 15 and 0 <= secs <= 59):
                return None
            if not (0 <= up <= 100 and 0 <= down <= 100):
                return None
            
            total_secs = mins * 60 + secs
            secs_left = 900 - total_secs
            
            return {
                'total_elapsed': total_secs,
                'secs_left': secs_left,
                'up': up / 100.0,
                'down': down / 100.0
            }
    except:
        pass
    return None


def analyze_window(folder_path):
    """
    Analyze one window:
    1. Skip if 100c hit before last 2 min
    2. Check if 90-99c appears in last 2 min
    3. Determine winner
    """
    folder_name = os.path.basename(folder_path)
    file_path = os.path.join(folder_path, folder_name + ".txt")
    
    if not os.path.exists(file_path):
        return {'error': 'file_not_found'}
    
    try:
        # Read all lines
        with open(file_path, 'r', encoding='utf-8', errors='ignore') as f:
            lines = f.readlines()
        
        # Parse all ticks
        all_ticks = []
        for line in lines:
            tick = parse_tick_line(line)
            if tick:
                all_ticks.append(tick)
        
        if len(all_ticks) < 10:
            return {'error': 'too_few_ticks'}
        
        # Separate into early and late
        early_ticks = [t for t in all_ticks if t['secs_left'] > 120]
        late_ticks = [t for t in all_ticks if 0 <= t['secs_left'] <= 120]
        
        # Check if 100c hit early (before last 2 min)
        if early_ticks:
            max_up_early = max(t['up'] for t in early_ticks)
            max_down_early = max(t['down'] for t in early_ticks)
            
            if max_up_early >= 1.00 or max_down_early >= 1.00:
                return {'skip': 'decided_early'}
        
        # Check if we have late data
        if not late_ticks:
            return {'error': 'no_late_ticks'}
        
        # Check for 90-99c in last 2 min
        up_90_99 = False
        down_90_99 = False
        
        for tick in late_ticks:
            if 0.90 <= tick['up'] <= 0.99:
                up_90_99 = True
            if 0.90 <= tick['down'] <= 0.99:
                down_90_99 = True
        
        # Determine winner from ALL ticks
        max_up_overall = max(t['up'] for t in all_ticks)
        max_down_overall = max(t['down'] for t in all_ticks)
        
        if max_up_overall >= 0.95 and max_up_overall > max_down_overall:
            winner = 'up'
        elif max_down_overall >= 0.95 and max_down_overall > max_up_overall:
            winner = 'down'
        else:
            winner = None
        
        return {
            'folder': folder_name,
            'total_ticks': len(all_ticks),
            'late_ticks': len(late_ticks),
            'up_90_99_late': up_90_99,
            'down_90_99_late': down_90_99,
            'winner': winner,
            'max_up': max_up_overall,
            'max_down': max_down_overall
        }
        
    except Exception as e:
        return {'error': str(e)[:50]}


def run_full_analysis():
    print("=" * 70)
    print("COMPLETE 50-DAY ANALYSIS - LAST 2 MINUTES")
    print("=" * 70)
    print("Analyzing ALL 4,872 windows carefully...")
    print("=" * 70)
    print()
    
    folders = sorted([d for d in os.listdir(DATA_DIR) 
                     if os.path.isdir(os.path.join(DATA_DIR, d))])
    
    # Counters
    file_not_found = 0
    too_few_ticks = 0
    decided_early = 0
    no_late_ticks = 0
    unclear_winner = 0
    other_errors = 0
    
    valid_windows = 0
    
    up_90_99_late = 0
    down_90_99_late = 0
    
    up_90_99_up_won = 0
    up_90_99_down_won = 0
    down_90_99_down_won = 0
    down_90_99_up_won = 0
    
    reversals_up = []
    reversals_down = []
    
    for i, folder in enumerate(folders):
        result = analyze_window(os.path.join(DATA_DIR, folder))
        
        # Handle errors/skips
        if 'error' in result:
            if result['error'] == 'file_not_found':
                file_not_found += 1
            elif result['error'] == 'too_few_ticks':
                too_few_ticks += 1
            elif result['error'] == 'no_late_ticks':
                no_late_ticks += 1
            else:
                other_errors += 1
            continue
        
        if 'skip' in result:
            if result['skip'] == 'decided_early':
                decided_early += 1
            continue
        
        if not result['winner']:
            unclear_winner += 1
            continue
        
        valid_windows += 1
        winner = result['winner']
        
        # Count touches in last 2 min
        if result['up_90_99_late']:
            up_90_99_late += 1
            
            if winner == 'up':
                up_90_99_up_won += 1
            else:  # DOWN won
                up_90_99_down_won += 1
                if len(reversals_up) < 10:
                    reversals_up.append(result)
        
        if result['down_90_99_late']:
            down_90_99_late += 1
            
            if winner == 'down':
                down_90_99_down_won += 1
            else:  # UP won
                down_90_99_up_won += 1
                if len(reversals_down) < 10:
                    reversals_down.append(result)
        
        # Progress
        if (i+1) % 1000 == 0:
            print(f"  {i+1}/{len(folders)}: Valid={valid_windows}, UP touches={up_90_99_late}, DOWN touches={down_90_99_late}")
    
    # Summary
    print()
    print("=" * 70)
    print("PROCESSING SUMMARY")
    print("=" * 70)
    print(f"Total folders: {len(folders)}")
    print(f"  File not found: {file_not_found}")
    print(f"  Too few ticks: {too_few_ticks}")
    print(f"  Decided early (100c): {decided_early}")
    print(f"  No late ticks: {no_late_ticks}")
    print(f"  Unclear winner: {unclear_winner}")
    print(f"  Other errors: {other_errors}")
    print(f"  VALID for analysis: {valid_windows}")
    print()
    
    print("=" * 70)
    print("90-99c IN LAST 2 MINUTES")
    print("=" * 70)
    print(f"UP 90-99c in last 2min:   {up_90_99_late} ({up_90_99_late/valid_windows*100:.1f}% of valid windows)")
    print(f"DOWN 90-99c in last 2min: {down_90_99_late} ({down_90_99_late/valid_windows*100:.1f}% of valid windows)")
    print()
    
    print("=" * 70)
    print("REVERSALS (90-99c but other side wins)")
    print("=" * 70)
    print(f"UP 90-99c but DOWN won:   {up_90_99_down_won:4} out of {up_90_99_late} ({up_90_99_down_won/up_90_99_late*100:.2f}% reversal)")
    print(f"DOWN 90-99c but UP won:   {down_90_99_up_won:4} out of {down_90_99_late} ({down_90_99_up_won/down_90_99_late*100:.2f}% reversal)")
    print()
    
    total_touches = up_90_99_late + down_90_99_late
    total_reversals = up_90_99_down_won + down_90_99_up_won
    total_wins = up_90_99_up_won + down_90_99_down_won
    
    print(f"TOTAL REVERSALS: {total_reversals} out of {total_touches} ({total_reversals/total_touches*100:.2f}%)")
    print(f"WIN RATE: {total_wins} out of {total_touches} ({total_wins/total_touches*100:.2f}%)")
    print()
    
    print("=" * 70)
    print("SAFE ENTRIES")
    print("=" * 70)
    print(f"UP 90-99c and UP won:     {up_90_99_up_won:4} ({up_90_99_up_won/up_90_99_late*100:.2f}% success)")
    print(f"DOWN 90-99c and DOWN won: {down_90_99_down_won:4} ({down_90_99_down_won/down_90_99_late*100:.2f}% success)")
    print()
    
    if reversals_up:
        print("=" * 70)
        print("REVERSAL EXAMPLES (UP 90-99c but DOWN won):")
        print("=" * 70)
        for r in reversals_up[:5]:
            print(f"  {r['folder']}: UP max {r['max_up']*100:.0f}c, DOWN max {r['max_down']*100:.0f}c")
    
    if reversals_down:
        print()
        print("=" * 70)
        print("REVERSAL EXAMPLES (DOWN 90-99c but UP won):")
        print("=" * 70)
        for r in reversals_down[:5]:
            print(f"  {r['folder']}: UP max {r['max_up']*100:.0f}c, DOWN max {r['max_down']*100:.0f}c")
    
    print()
    print("=" * 70)
    print("FINAL VERDICT:")
    win_rate = (total_wins / total_touches * 100) if total_touches > 0 else 0
    print(f"LAST 2-MIN ENTRY WIN RATE: {win_rate:.2f}%")
    
    if win_rate >= 95:
        print("EXCELLENT - Very safe strategy")
    elif win_rate >= 90:
        print("GOOD - Profitable with proper sizing")
    elif win_rate >= 85:
        print("MODERATE - Need conservative sizing")
    else:
        print("POOR - Not recommended")
    print("=" * 70)


if __name__ == "__main__":
    run_full_analysis()





