"""Parse raw tick data from backtesting15mbitcoin repo."""
import re
import os
from dataclasses import dataclass
from typing import List, Optional, Tuple
from pathlib import Path


@dataclass
class RawTick:
    """A single raw tick from one file (BUY or SELL)."""
    elapsed_secs: float
    up_cents: int
    down_cents: int
    is_valid: bool = True


# Regex pattern for tick lines
TICK_PATTERN = re.compile(r"(\d{2}):(\d{2}):(\d{3}) - UP (-?\d+)C \| DOWN (-?\d+)C")


def parse_tick_line(line: str) -> Optional[RawTick]:
    """Parse a single tick line.
    
    Returns None if line doesn't match pattern.
    Returns RawTick with is_valid=False if UP or DOWN is -1.
    """
    match = TICK_PATTERN.match(line.strip())
    if not match:
        return None
    
    minutes = int(match.group(1))
    seconds = int(match.group(2))
    millis = int(match.group(3))
    
    elapsed = minutes * 60 + seconds + millis / 1000.0
    up_cents = int(match.group(4))
    down_cents = int(match.group(5))
    
    # Invalid if either is -1
    is_valid = (up_cents >= 0 and down_cents >= 0)
    
    return RawTick(
        elapsed_secs=elapsed,
        up_cents=up_cents,
        down_cents=down_cents,
        is_valid=is_valid
    )


def parse_tick_file(filepath: str) -> List[RawTick]:
    """Parse a tick file with safety guards.
    
    Stops parsing when:
    - UP or DOWN == -1 (invalid tick)
    - elapsed > 901 seconds
    - elapsed < previous elapsed (time reset / contamination)
    """
    ticks = []
    prev_elapsed = -1.0
    
    try:
        with open(filepath, 'r', encoding='utf-8') as f:
            for line in f:
                tick = parse_tick_line(line)
                if tick is None:
                    continue
                
                # Stop on invalid tick
                if not tick.is_valid:
                    break
                
                # Stop if elapsed > 901s
                if tick.elapsed_secs > 901.0:
                    break
                
                # Stop if time goes backwards (contamination)
                if tick.elapsed_secs < prev_elapsed - 0.5:  # 0.5s tolerance
                    break
                
                ticks.append(tick)
                prev_elapsed = tick.elapsed_secs
                
    except Exception as e:
        print(f"Error parsing {filepath}: {e}")
    
    return ticks


def find_window_ids(data_dir: str) -> List[str]:
    """Find all window IDs in a data directory.
    
    Each window is a folder like: 25_10_31_00_00_00_15
    """
    window_ids = []
    
    if not os.path.exists(data_dir):
        return window_ids
    
    for entry in os.listdir(data_dir):
        entry_path = os.path.join(data_dir, entry)
        if os.path.isdir(entry_path):
            # Check if there's a matching .txt file inside
            txt_file = os.path.join(entry_path, f"{entry}.txt")
            if os.path.exists(txt_file):
                window_ids.append(entry)
    
    return sorted(window_ids)


def load_window_ticks(
    window_id: str,
    buy_dir: str,
    sell_dir: str
) -> Tuple[List[RawTick], List[RawTick]]:
    """Load both BUY and SELL ticks for a window.
    
    Returns:
        (buy_ticks, sell_ticks) - may be empty lists if files don't exist
    """
    buy_file = os.path.join(buy_dir, window_id, f"{window_id}.txt")
    sell_file = os.path.join(sell_dir, window_id, f"{window_id}.txt")
    
    buy_ticks = parse_tick_file(buy_file) if os.path.exists(buy_file) else []
    sell_ticks = parse_tick_file(sell_file) if os.path.exists(sell_file) else []
    
    return buy_ticks, sell_ticks


def parse_window_id_to_datetime(window_id: str) -> Optional[str]:
    """Parse window ID to ISO datetime string.
    
    Format: YY_MM_DD_HH_MM_HH_MM
    Example: 25_10_31_00_00_00_15 -> 2025-10-31T00:00:00
    """
    try:
        parts = window_id.split('_')
        if len(parts) != 7:
            return None
        yy, mm, dd, hh1, min1, hh2, min2 = parts
        return f"20{yy}-{mm}-{dd}T{hh1}:{min1}:00"
    except:
        return None


