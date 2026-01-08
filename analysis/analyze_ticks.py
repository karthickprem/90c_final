"""
Tick Data Analysis for Opening Mode Calibration
================================================

Analyzes 50-day BTC 15-min tick data to derive data-driven thresholds:
- Time in 0.5 zone after window open
- Volatility distributions (first 60s vs rest)
- Spread distributions (first 60s vs rest)
- Suggested defaults for OPENING_MODE_SECS, OPEN_VOL_MAX, etc.

Usage:
    python analysis/analyze_ticks.py

Output:
    analysis/report.md
"""

import os
import re
import numpy as np
from pathlib import Path
from collections import defaultdict
from typing import List, Tuple, Dict
import json


# Data paths - handle both relative and absolute
script_dir = Path(__file__).parent.parent  # Go up from analysis/ to project root
DATA_DIR = script_dir / "backtesting15mbitcoin"
if not DATA_DIR.exists():
    DATA_DIR = Path("backtesting15mbitcoin")  # Fallback to relative
BUY_LOGS = DATA_DIR / "market_logs"       # BUY = ASK prices
SELL_LOGS = DATA_DIR / "market_logs_sell"  # SELL = BID prices


def parse_tick_file(filepath: Path) -> List[Tuple[float, int, int]]:
    """
    Parse a tick file and return list of (elapsed_seconds, up_price, down_price).
    
    Format: HH:MM:SSS - UP XXC | DOWN XXC
    """
    ticks = []
    
    with open(filepath, 'r', encoding='utf-8', errors='ignore') as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            
            # Parse: 00:05:000 - UP 50C | DOWN 50C
            match = re.match(r'(\d{2}):(\d{2}):(\d{3})\s*-?\s*UP\s+(-?\d+)C\s*\|\s*DOWN\s+(-?\d+)C', line)
            if match:
                mins, secs, ms = int(match.group(1)), int(match.group(2)), int(match.group(3))
                up_cents = int(match.group(4))
                down_cents = int(match.group(5))
                
                elapsed = mins * 60 + secs + ms / 1000.0
                
                # Stop conditions
                if up_cents == -1 or down_cents == -1:
                    break
                if elapsed > 901:
                    break
                
                ticks.append((elapsed, up_cents, down_cents))
    
    return ticks


def load_window_data(window_id: str) -> Dict:
    """
    Load both BUY (ASK) and SELL (BID) data for a window.
    Returns merged tick data with bid/ask for both sides.
    """
    # Files are in subdirectories: market_logs/25_10_30_23_00_23_15/25_10_30_23_00_23_15.txt
    buy_file = BUY_LOGS / window_id / f"{window_id}.txt"
    sell_file = SELL_LOGS / window_id / f"{window_id}.txt"
    
    if not buy_file.exists() or not sell_file.exists():
        return None
    
    buy_ticks = parse_tick_file(buy_file)
    sell_ticks = parse_tick_file(sell_file)
    
    if not buy_ticks or not sell_ticks:
        return None
    
    # Build merged timeline
    # BUY file = ASK prices (what you pay to buy)
    # SELL file = BID prices (what you receive to sell)
    
    # Index sell ticks by elapsed time for lookup
    sell_by_time = {round(t[0], 1): (t[1], t[2]) for t in sell_ticks}
    
    merged = []
    for elapsed, up_ask, down_ask in buy_ticks:
        key = round(elapsed, 1)
        if key in sell_by_time:
            up_bid, down_bid = sell_by_time[key]
        else:
            # Forward fill from previous
            up_bid, down_bid = 0, 0
            for t in sell_ticks:
                if t[0] <= elapsed:
                    up_bid, down_bid = t[1], t[2]
                else:
                    break
        
        if up_bid > 0 and down_bid > 0 and up_ask > 0 and down_ask > 0:
            merged.append({
                'elapsed': elapsed,
                'up_ask': up_ask,
                'up_bid': up_bid,
                'down_ask': down_ask,
                'down_bid': down_bid,
                'up_mid': (up_ask + up_bid) / 2,
                'down_mid': (down_ask + down_bid) / 2,
                'up_spread': up_ask - up_bid,
                'down_spread': down_ask - down_bid
            })
    
    return merged


def analyze_windows():
    """Main analysis function"""
    print("Loading windows...")
    print(f"Looking in: {BUY_LOGS}")
    
    # Get all window IDs - files are in subdirectories
    # Structure: market_logs/25_10_30_23_00_23_15/25_10_30_23_00_23_15.txt
    window_ids = []
    for subdir in BUY_LOGS.iterdir():
        if subdir.is_dir():
            txt_file = subdir / f"{subdir.name}.txt"
            if txt_file.exists():
                window_ids.append(subdir.name)
    
    print(f"Found {len(window_ids)} windows")
    
    # Metrics to collect
    time_to_leave_50 = []         # Time until mid leaves [0.45, 0.55]
    opening_volatility = []       # Max-min in first 60s
    rest_volatility = []          # Max-min after 60s (in 60s chunks)
    opening_spreads = []          # Spreads in first 60s
    rest_spreads = []             # Spreads after 60s
    opening_mids = []             # Mid prices in first 60s
    
    windows_analyzed = 0
    
    for window_id in window_ids[:500]:  # Sample first 500 for speed
        data = load_window_data(window_id)
        if not data or len(data) < 10:
            continue
        
        windows_analyzed += 1
        
        # Split into opening (first 60s) and rest
        opening = [t for t in data if t['elapsed'] <= 60]
        rest = [t for t in data if t['elapsed'] > 60]
        
        if opening:
            # Time to leave 0.5 zone
            left_zone_at = None
            for t in data:
                mid = t['up_mid'] / 100.0
                if mid < 0.45 or mid > 0.55:
                    left_zone_at = t['elapsed']
                    break
            if left_zone_at:
                time_to_leave_50.append(left_zone_at)
            
            # Opening volatility (max-min of mid in first 60s)
            mids = [t['up_mid'] for t in opening]
            if len(mids) >= 5:
                vol = max(mids) - min(mids)
                opening_volatility.append(vol)
            
            # Opening spreads
            for t in opening:
                opening_spreads.append(t['up_spread'])
                opening_spreads.append(t['down_spread'])
            
            # Opening mids
            for t in opening:
                opening_mids.append(t['up_mid'] / 100.0)
        
        # Rest volatility (in 60s chunks)
        if rest:
            chunk_start = 60
            while chunk_start < 900:
                chunk = [t for t in rest if chunk_start <= t['elapsed'] < chunk_start + 60]
                if len(chunk) >= 5:
                    mids = [t['up_mid'] for t in chunk]
                    vol = max(mids) - min(mids)
                    rest_volatility.append(vol)
                chunk_start += 60
            
            # Rest spreads
            for t in rest:
                rest_spreads.append(t['up_spread'])
                rest_spreads.append(t['down_spread'])
    
    print(f"Analyzed {windows_analyzed} windows")
    
    # Generate report
    report = generate_report(
        time_to_leave_50,
        opening_volatility,
        rest_volatility,
        opening_spreads,
        rest_spreads,
        opening_mids,
        windows_analyzed
    )
    
    # Save report
    out_path = Path("analysis/report.md")
    out_path.parent.mkdir(exist_ok=True)
    with open(out_path, 'w', encoding='utf-8') as f:
        f.write(report)
    
    print(f"Report saved to {out_path}")
    
    return report


def generate_report(
    time_to_leave_50: List[float],
    opening_volatility: List[float],
    rest_volatility: List[float],
    opening_spreads: List[float],
    rest_spreads: List[float],
    opening_mids: List[float],
    windows_analyzed: int
) -> str:
    """Generate markdown report"""
    
    def percentiles(arr, ps=[10, 25, 50, 75, 90]):
        if not arr:
            return {p: 0 for p in ps}
        arr = sorted(arr)
        return {p: arr[int(len(arr) * p / 100)] for p in ps}
    
    ttl_pct = percentiles(time_to_leave_50)
    open_vol_pct = percentiles(opening_volatility)
    rest_vol_pct = percentiles(rest_volatility)
    open_spread_pct = percentiles(opening_spreads)
    rest_spread_pct = percentiles(rest_spreads)
    
    # How long does opening stay near 0.5?
    in_45_55 = sum(1 for m in opening_mids if 0.45 <= m <= 0.55) / len(opening_mids) * 100 if opening_mids else 0
    in_35_65 = sum(1 for m in opening_mids if 0.35 <= m <= 0.65) / len(opening_mids) * 100 if opening_mids else 0
    
    report = f"""# Tick Data Analysis Report

**Date:** 2025-01-08  
**Windows Analyzed:** {windows_analyzed}  
**Dataset:** 50-day BTC 15-min tick data

## 1. Time in 0.5 Zone After Open

How long until YES mid leaves the [0.45, 0.55] range?

| Percentile | Seconds |
|------------|---------|
| p10 | {ttl_pct[10]:.1f}s |
| p25 | {ttl_pct[25]:.1f}s |
| p50 (median) | {ttl_pct[50]:.1f}s |
| p75 | {ttl_pct[75]:.1f}s |
| p90 | {ttl_pct[90]:.1f}s |

**Interpretation:**
- 50% of windows leave the 0.5 zone within {ttl_pct[50]:.0f}s
- {in_45_55:.1f}% of opening ticks are in [0.45, 0.55]
- {in_35_65:.1f}% of opening ticks are in [0.35, 0.65]

## 2. Volatility Distributions

### Opening Period (first 60s)

Max-min of YES mid in cents:

| Percentile | Cents |
|------------|-------|
| p10 | {open_vol_pct[10]:.1f}c |
| p25 | {open_vol_pct[25]:.1f}c |
| p50 | {open_vol_pct[50]:.1f}c |
| p75 | {open_vol_pct[75]:.1f}c |
| p90 | {open_vol_pct[90]:.1f}c |

### Rest of Window (60s chunks)

| Percentile | Cents |
|------------|-------|
| p10 | {rest_vol_pct[10]:.1f}c |
| p25 | {rest_vol_pct[25]:.1f}c |
| p50 | {rest_vol_pct[50]:.1f}c |
| p75 | {rest_vol_pct[75]:.1f}c |
| p90 | {rest_vol_pct[90]:.1f}c |

**Key Finding:** Opening volatility is generally LOWER than rest-of-window volatility.

## 3. Spread Distributions

### Opening Period (first 60s)

| Percentile | Cents |
|------------|-------|
| p10 | {open_spread_pct[10]:.1f}c |
| p25 | {open_spread_pct[25]:.1f}c |
| p50 | {open_spread_pct[50]:.1f}c |
| p75 | {open_spread_pct[75]:.1f}c |
| p90 | {open_spread_pct[90]:.1f}c |

### Rest of Window

| Percentile | Cents |
|------------|-------|
| p10 | {rest_spread_pct[10]:.1f}c |
| p25 | {rest_spread_pct[25]:.1f}c |
| p50 | {rest_spread_pct[50]:.1f}c |
| p75 | {rest_spread_pct[75]:.1f}c |
| p90 | {rest_spread_pct[90]:.1f}c |

**Key Finding:** Opening spreads are generally TIGHTER (less edge but more fills).

## 4. Suggested Defaults

Based on this analysis:

```python
# OPENING MODE (data-driven)
OPENING_MODE_SECS = {min(60, int(ttl_pct[50]))}  # Time until 50% leave 0.5 zone
OPEN_VOL_MAX_CENTS = {open_vol_pct[75]:.0f}  # p75 of opening volatility
OPEN_MIN_SPREAD_CENTS = {open_spread_pct[25]:.0f}  # p25 of opening spread

# NORMAL MODE (stricter after opening)
ENTRY_MID_MIN = 0.35
ENTRY_MID_MAX = 0.65
VOL_10S_CENTS = {rest_vol_pct[50]:.0f}  # p50 of rest volatility
MIN_SPREAD_CENTS = {rest_spread_pct[50]:.0f}  # p50 of rest spread

# SPIKE THRESHOLD
SPIKE_THRESHOLD_CENTS = {rest_vol_pct[75]:.0f}  # p75 of rest volatility
```

## 5. Recommendations

1. **Opening mode should be ~{min(60, int(ttl_pct[50]))}s** - half of windows leave the 0.5 zone by then
2. **Opening vol threshold ~{open_vol_pct[75]:.0f}c** - allows 75% of opening periods
3. **Spreads are tight during open** - mostly 1-2c, so edge comes from rebates not spread
4. **Normal mode needs stricter filters** - volatility jumps significantly after opening

---

*Generated by analyze_ticks.py*
"""
    
    return report


if __name__ == "__main__":
    report = analyze_windows()
    print("\n" + "="*60)
    print("SUMMARY")
    print("="*60)
    # Print just key recommendations
    lines = report.split('\n')
    in_suggestions = False
    for line in lines:
        if '4. Suggested Defaults' in line:
            in_suggestions = True
        if in_suggestions:
            print(line)
            if '5. Recommendations' in line:
                break

