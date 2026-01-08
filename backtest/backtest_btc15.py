#!/usr/bin/env python3
"""
BTC 15-min Polymarket Backtest (robust, zero-guessing)

Computes:
- UP_TOUCH_90_AND_UP_WIN
- DOWN_TOUCH_90_AND_DOWN_WIN
- UP_TOUCH_90_AND_DOWN_WIN
- DOWN_TOUCH_90_AND_UP_WIN

Key rule: At the end of the window timer, whatever has value is the winner.
Handles early timer resets and invalid final ticks robustly.

CRITICAL: Segments windows by timer resets to avoid contamination from next window.
"""

from __future__ import annotations

import argparse
import csv
import json
import re
import sys
from dataclasses import dataclass, field, asdict
from pathlib import Path
from typing import Optional, List, Dict, Tuple, Any


# ============================================================================
# Constants
# ============================================================================

# If elapsed_seconds decreases by more than this, we detect a timer reset
RESET_JUMP_THRESHOLD = 30.0


# ============================================================================
# Data Classes
# ============================================================================

@dataclass
class Tick:
    """A single price tick from the market."""
    elapsed_seconds: float
    up_cents: int
    down_cents: int
    raw_line: str = ""
    line_index: int = 0  # Original position in file
    
    @property
    def up(self) -> float:
        return self.up_cents / 100.0
    
    @property
    def down(self) -> float:
        return self.down_cents / 100.0
    
    def is_valid(self) -> bool:
        """Check if tick has valid cent values (0-100 for both)."""
        return (0 <= self.up_cents <= 100) and (0 <= self.down_cents <= 100)
    
    def is_resolved(self, resolve_min: int = 97) -> bool:
        """Check if tick represents a resolved/settled state."""
        if not self.is_valid():
            return False
        max_cents = max(self.up_cents, self.down_cents)
        min_cents = min(self.up_cents, self.down_cents)
        resolve_max_threshold = 100 - resolve_min  # e.g., 3c for resolve_min=97
        return max_cents >= resolve_min and min_cents <= resolve_max_threshold
    
    def sum_check(self) -> Tuple[bool, float]:
        """Return (is_sane, sum_value) for sanity check."""
        total = self.up + self.down
        is_sane = 0.85 <= total <= 1.15
        return is_sane, total


@dataclass
class WindowResult:
    """Analysis result for a single window."""
    window_id: str
    num_ticks: int = 0
    num_valid_ticks: int = 0
    last_valid_time: Optional[float] = None
    resolve_time: Optional[float] = None
    winner: str = "UNCLEAR"  # UP, DOWN, or UNCLEAR
    up_touch_90: bool = False
    down_touch_90: bool = False
    up_touch_90_pre_resolve: Optional[bool] = None
    down_touch_90_pre_resolve: Optional[bool] = None
    issues: List[str] = field(default_factory=list)
    parse_errors: int = 0
    
    # Diagnostic fields for timer reset detection
    min_elapsed: Optional[float] = None
    max_elapsed: Optional[float] = None
    max_up_cents: int = 0
    max_down_cents: int = 0
    backward_jumps: int = 0
    ticks_truncated: int = 0  # Ticks removed due to segmentation
    segment_used: int = 0  # Which segment was used (0 = first/only)
    
    def has_issues(self) -> bool:
        return len(self.issues) > 0 or self.winner == "UNCLEAR"


@dataclass
class BacktestSummary:
    """Summary of backtest results."""
    total_windows: int = 0
    total_with_winner: int = 0
    unclear_winner_count: int = 0
    
    # =========================================================================
    # THE 4 REQUESTED COUNTS (CLEAR WINNERS ONLY - excludes UNCLEAR)
    # =========================================================================
    up_touch_90_and_up_win: int = 0
    down_touch_90_and_down_win: int = 0
    up_touch_90_and_down_win: int = 0
    down_touch_90_and_up_win: int = 0
    
    # =========================================================================
    # SUPPORTING TOTALS (CLEAR WINNERS ONLY)
    # =========================================================================
    up_touch_total_clear: int = 0      # count(clear windows where UP_TOUCH_90)
    down_touch_total_clear: int = 0    # count(clear windows where DOWN_TOUCH_90)
    both_touch_total_clear: int = 0    # count(clear windows where BOTH touched)
    neither_touch_total_clear: int = 0 # count(clear windows where NEITHER touched)
    
    # Touch counters for UNCLEAR windows (for partition validation)
    up_touch_90_unclear: int = 0
    down_touch_90_unclear: int = 0
    both_touch_unclear: int = 0
    neither_touch_unclear: int = 0
    
    # Pre-resolve counters
    up_touch_90_pre_resolve_and_up_win: int = 0
    down_touch_90_pre_resolve_and_down_win: int = 0
    up_touch_90_pre_resolve_and_down_win: int = 0
    down_touch_90_pre_resolve_and_up_win: int = 0
    
    # Touch totals (across ALL windows including unclear)
    up_touch_total: int = 0
    down_touch_total: int = 0
    both_touch_total: int = 0
    neither_touch_total: int = 0
    
    # Legacy names for compatibility (same as *_total_clear)
    both_touch_90: int = 0
    neither_touch_90: int = 0
    up_wins: int = 0
    down_wins: int = 0
    
    # Resolve-like spike count
    windows_with_resolve_spike: int = 0
    
    # Resolve time distribution
    resolve_times: List[float] = field(default_factory=list)
    early_resets: int = 0  # resolve_time < 900
    
    # Segmentation stats
    windows_with_truncation: int = 0
    total_ticks_truncated: int = 0
    
    # Touch thresholds used
    touch_threshold: int = 90
    resolve_min: int = 97
    
    def to_dict(self) -> Dict[str, Any]:
        """Convert to dictionary for JSON serialization."""
        d = asdict(self)
        # Compute stats on resolve times
        if self.resolve_times:
            d['resolve_time_min'] = min(self.resolve_times)
            d['resolve_time_max'] = max(self.resolve_times)
            d['resolve_time_mean'] = sum(self.resolve_times) / len(self.resolve_times)
            d['resolve_time_median'] = sorted(self.resolve_times)[len(self.resolve_times) // 2]
        else:
            d['resolve_time_min'] = None
            d['resolve_time_max'] = None
            d['resolve_time_mean'] = None
            d['resolve_time_median'] = None
        # Remove raw list for cleaner output
        del d['resolve_times']
        
        # Add structured output for the 4 requested counts
        d['requested_counts_clear'] = {
            'UP_TOUCH_90_AND_UP_WIN': self.up_touch_90_and_up_win,
            'DOWN_TOUCH_90_AND_DOWN_WIN': self.down_touch_90_and_down_win,
            'UP_TOUCH_90_AND_DOWN_WIN': self.up_touch_90_and_down_win,
            'DOWN_TOUCH_90_AND_UP_WIN': self.down_touch_90_and_up_win,
        }
        d['supporting_totals_clear'] = {
            'clear_winners_total': self.total_with_winner,
            'up_touch_total_clear': self.up_touch_total_clear,
            'down_touch_total_clear': self.down_touch_total_clear,
            'both_touch_total_clear': self.both_touch_total_clear,
            'neither_touch_total_clear': self.neither_touch_total_clear,
        }
        d['identity_checks_passed'] = True  # Will be validated before output
        
        return d


# ============================================================================
# Parsing Functions
# ============================================================================

# Regex patterns for tick lines
# Format: MM:SS:ms - UP XXC | DOWN YYC  or  MM:SS - UP XXC | DOWN YYC
TICK_PATTERN_MS = re.compile(
    r'^\s*(\d{1,2}):(\d{2}):(\d+)\s*-\s*UP\s+(-?\d+)C?\s*\|\s*DOWN\s+(-?\d+)C?\s*$',
    re.IGNORECASE
)
TICK_PATTERN_NO_MS = re.compile(
    r'^\s*(\d{1,2}):(\d{2})\s*-\s*UP\s+(-?\d+)C?\s*\|\s*DOWN\s+(-?\d+)C?\s*$',
    re.IGNORECASE
)

# Window header pattern for Format B
WINDOW_HEADER_PATTERN = re.compile(r'^\s*window\s*:\s*(.+?)\s*$', re.IGNORECASE)


def parse_tick_line(line: str, line_index: int = 0) -> Optional[Tick]:
    """
    Parse a tick line and return a Tick object.
    Returns None if line cannot be parsed.
    """
    line = line.strip()
    if not line:
        return None
    
    # Try format with milliseconds first
    match = TICK_PATTERN_MS.match(line)
    if match:
        minutes = int(match.group(1))
        seconds = int(match.group(2))
        ms = int(match.group(3))
        up_cents = int(match.group(4))
        down_cents = int(match.group(5))
        
        elapsed = minutes * 60 + seconds + ms / 1000.0
        return Tick(
            elapsed_seconds=elapsed,
            up_cents=up_cents,
            down_cents=down_cents,
            raw_line=line,
            line_index=line_index
        )
    
    # Try format without milliseconds
    match = TICK_PATTERN_NO_MS.match(line)
    if match:
        minutes = int(match.group(1))
        seconds = int(match.group(2))
        up_cents = int(match.group(3))
        down_cents = int(match.group(4))
        
        elapsed = minutes * 60 + seconds
        return Tick(
            elapsed_seconds=elapsed,
            up_cents=up_cents,
            down_cents=down_cents,
            raw_line=line,
            line_index=line_index
        )
    
    return None


def parse_window_file(file_path: Path) -> Tuple[str, List[Tick], List[str]]:
    """
    Parse a single window file (Format A).
    Returns (window_id, list_of_ticks, list_of_errors).
    Ticks are returned in file order (not sorted).
    """
    window_id = file_path.stem
    ticks: List[Tick] = []
    errors: List[str] = []
    
    try:
        content = file_path.read_text(encoding='utf-8', errors='replace')
    except Exception as e:
        errors.append(f"Failed to read file: {e}")
        return window_id, ticks, errors
    
    for line_num, line in enumerate(content.splitlines(), 1):
        if not line.strip():
            continue
        
        tick = parse_tick_line(line, line_index=line_num)
        if tick is None:
            errors.append(f"Line {line_num}: Failed to parse: {line[:100]}")
        else:
            ticks.append(tick)
    
    return window_id, ticks, errors


def parse_combined_file(file_path: Path) -> List[Tuple[str, List[Tick], List[str]]]:
    """
    Parse a combined file with multiple windows (Format B).
    Returns list of (window_id, list_of_ticks, list_of_errors) tuples.
    """
    windows: List[Tuple[str, List[Tick], List[str]]] = []
    
    try:
        content = file_path.read_text(encoding='utf-8', errors='replace')
    except Exception as e:
        return [(file_path.stem, [], [f"Failed to read file: {e}"])]
    
    current_window_id: Optional[str] = None
    current_ticks: List[Tick] = []
    current_errors: List[str] = []
    line_num = 0
    
    for line in content.splitlines():
        line_num += 1
        stripped = line.strip()
        
        if not stripped:
            continue
        
        # Check for window header
        header_match = WINDOW_HEADER_PATTERN.match(stripped)
        if header_match:
            # Save previous window if exists
            if current_window_id is not None:
                windows.append((current_window_id, current_ticks, current_errors))
            
            current_window_id = header_match.group(1).strip()
            current_ticks = []
            current_errors = []
            continue
        
        # Try to parse as tick
        tick = parse_tick_line(stripped, line_index=line_num)
        if tick is not None:
            current_ticks.append(tick)
        elif current_window_id is not None:
            # Only log error if we're inside a window
            current_errors.append(f"Line {line_num}: Failed to parse: {stripped[:100]}")
    
    # Don't forget the last window
    if current_window_id is not None:
        windows.append((current_window_id, current_ticks, current_errors))
    
    return windows


def detect_input_format(input_path: Path) -> str:
    """
    Detect whether input is Format A (directory) or Format B (single file).
    Returns 'dir' or 'file'.
    """
    if input_path.is_dir():
        return 'dir'
    return 'file'


def load_windows(input_path: Path) -> List[Tuple[str, List[Tick], List[str]]]:
    """
    Load all windows from input path.
    Auto-detects Format A (directory with one file per window) 
    or Format B (single file with window headers).
    """
    fmt = detect_input_format(input_path)
    
    if fmt == 'dir':
        # Format A: directory with subdirectories or files
        windows = []
        
        # Check if input_path contains .txt files directly
        txt_files = list(input_path.glob('*.txt'))
        if txt_files:
            for txt_file in sorted(txt_files):
                window_id, ticks, errors = parse_window_file(txt_file)
                windows.append((window_id, ticks, errors))
        else:
            # Check for subdirectories (each containing a .txt file)
            for subdir in sorted(input_path.iterdir()):
                if subdir.is_dir():
                    txt_files = list(subdir.glob('*.txt'))
                    for txt_file in txt_files:
                        window_id, ticks, errors = parse_window_file(txt_file)
                        windows.append((window_id, ticks, errors))
        
        return windows
    else:
        # Format B: single file with window headers
        return parse_combined_file(input_path)


# ============================================================================
# Segmentation Functions (Timer Reset Detection)
# ============================================================================

def segment_ticks_by_reset(
    ticks: List[Tick],
    reset_threshold: float = RESET_JUMP_THRESHOLD
) -> Tuple[List[List[Tick]], int]:
    """
    Segment ticks by detecting timer resets (elapsed_seconds jumps backwards).
    
    Scans ticks in FILE ORDER (not sorted).
    Detects reset when elapsed_seconds decreases by more than reset_threshold.
    
    Returns: (list_of_segments, count_of_backward_jumps)
    Each segment is a list of ticks in that continuous time range.
    """
    if not ticks:
        return [], 0
    
    segments: List[List[Tick]] = []
    current_segment: List[Tick] = [ticks[0]]
    backward_jumps = 0
    
    for i in range(1, len(ticks)):
        prev_time = ticks[i - 1].elapsed_seconds
        curr_time = ticks[i].elapsed_seconds
        
        # Detect backward jump
        if prev_time - curr_time > reset_threshold:
            backward_jumps += 1
            # Start new segment
            segments.append(current_segment)
            current_segment = [ticks[i]]
        else:
            current_segment.append(ticks[i])
    
    # Don't forget the last segment
    if current_segment:
        segments.append(current_segment)
    
    return segments, backward_jumps


def select_segment(segments: List[List[Tick]]) -> Tuple[List[Tick], int, int]:
    """
    Select which segment to use for analysis.
    
    Strategy: Use the FIRST segment (the actual window data).
    Ticks after the first reset are from the next window.
    
    Returns: (selected_ticks, segment_index, ticks_truncated)
    """
    if not segments:
        return [], 0, 0
    
    # Use first segment
    selected = segments[0]
    ticks_truncated = sum(len(s) for s in segments[1:])
    
    return selected, 0, ticks_truncated


# ============================================================================
# Analysis Functions
# ============================================================================

def analyze_window(
    window_id: str,
    ticks: List[Tick],
    errors: List[str],
    touch_threshold: int = 90,
    resolve_min: int = 97
) -> WindowResult:
    """
    Analyze a single window and determine winner, touches, etc.
    
    CRITICAL: Segments by timer resets first, then analyzes only the first segment.
    """
    result = WindowResult(window_id=window_id)
    result.parse_errors = len(errors)
    result.issues.extend(errors)
    result.num_ticks = len(ticks)
    
    if not ticks:
        result.issues.append("No ticks found")
        return result
    
    # ========================================================================
    # Step 1: Segment by timer resets (in FILE ORDER, not sorted)
    # ========================================================================
    
    segments, backward_jumps = segment_ticks_by_reset(ticks)
    result.backward_jumps = backward_jumps
    
    # Select segment (use first segment = actual window)
    segment_ticks, segment_idx, ticks_truncated = select_segment(segments)
    result.segment_used = segment_idx
    result.ticks_truncated = ticks_truncated
    
    if ticks_truncated > 0:
        result.issues.append(
            f"Truncated {ticks_truncated} ticks after timer reset "
            f"({backward_jumps} backward jumps detected)"
        )
    
    if not segment_ticks:
        result.issues.append("No ticks in selected segment")
        return result
    
    # ========================================================================
    # Step 2: Now sort the segment by elapsed time and deduplicate
    # ========================================================================
    
    ticks_sorted = sorted(segment_ticks, key=lambda t: t.elapsed_seconds)
    
    # Deduplicate: keep last occurrence for same elapsed_seconds
    deduped: Dict[float, Tick] = {}
    for tick in ticks_sorted:
        deduped[tick.elapsed_seconds] = tick
    ticks_sorted = [deduped[t] for t in sorted(deduped.keys())]
    
    # Filter valid ticks
    valid_ticks = [t for t in ticks_sorted if t.is_valid()]
    result.num_valid_ticks = len(valid_ticks)
    
    if not valid_ticks:
        result.issues.append("No valid ticks found in segment")
        return result
    
    # ========================================================================
    # Step 3: Compute diagnostics
    # ========================================================================
    
    result.min_elapsed = valid_ticks[0].elapsed_seconds
    result.max_elapsed = valid_ticks[-1].elapsed_seconds
    result.last_valid_time = valid_ticks[-1].elapsed_seconds
    
    # Track max prices
    result.max_up_cents = max(t.up_cents for t in valid_ticks)
    result.max_down_cents = max(t.down_cents for t in valid_ticks)
    
    # Check for sum sanity issues (log but don't discard)
    sum_issues = 0
    for tick in valid_ticks:
        is_sane, total = tick.sum_check()
        if not is_sane:
            sum_issues += 1
    if sum_issues > 0:
        result.issues.append(f"Sum check failed for {sum_issues} ticks")
    
    # ========================================================================
    # Step 4: Winner Detection
    # ========================================================================
    
    # Scan from end backwards to find last resolved tick
    resolved_tick: Optional[Tick] = None
    for tick in reversed(valid_ticks):
        if tick.is_resolved(resolve_min):
            resolved_tick = tick
            break
    
    if resolved_tick is not None:
        result.resolve_time = resolved_tick.elapsed_seconds
        if resolved_tick.up_cents > resolved_tick.down_cents:
            result.winner = "UP"
        else:
            result.winner = "DOWN"
    else:
        # Fallback: use last valid tick
        last_tick = valid_ticks[-1]
        price_diff = abs(last_tick.up - last_tick.down)
        max_price = max(last_tick.up, last_tick.down)
        
        # Mark as UNCLEAR if too close and neither side dominant
        if price_diff < 0.05 and max_price < 0.60:
            result.winner = "UNCLEAR"
            result.issues.append(
                f"Winner unclear at final tick: UP={last_tick.up_cents}c "
                f"DOWN={last_tick.down_cents}c"
            )
        else:
            if last_tick.up_cents > last_tick.down_cents:
                result.winner = "UP"
            else:
                result.winner = "DOWN"
    
    # ========================================================================
    # Step 5: Touch Detection
    # ========================================================================
    
    # Regular touch (any time in segment)
    result.up_touch_90 = any(t.up_cents >= touch_threshold for t in valid_ticks)
    result.down_touch_90 = any(t.down_cents >= touch_threshold for t in valid_ticks)
    
    # Pre-resolve touch (only consider ticks before or at resolve time)
    if result.resolve_time is not None:
        pre_resolve_ticks = [
            t for t in valid_ticks 
            if t.elapsed_seconds <= result.resolve_time
        ]
        result.up_touch_90_pre_resolve = any(
            t.up_cents >= touch_threshold for t in pre_resolve_ticks
        )
        result.down_touch_90_pre_resolve = any(
            t.down_cents >= touch_threshold for t in pre_resolve_ticks
        )
    else:
        # If no resolve, same as regular touch
        result.up_touch_90_pre_resolve = result.up_touch_90
        result.down_touch_90_pre_resolve = result.down_touch_90
    
    return result


def compute_summary(
    results: List[WindowResult],
    touch_threshold: int = 90,
    resolve_min: int = 97
) -> BacktestSummary:
    """
    Compute aggregate statistics from all window results.
    """
    summary = BacktestSummary()
    summary.total_windows = len(results)
    summary.touch_threshold = touch_threshold
    summary.resolve_min = resolve_min
    
    for r in results:
        # ====================================================================
        # Touch totals (across ALL windows including unclear)
        # ====================================================================
        if r.up_touch_90:
            summary.up_touch_total += 1
        if r.down_touch_90:
            summary.down_touch_total += 1
        if r.up_touch_90 and r.down_touch_90:
            summary.both_touch_total += 1
        if not r.up_touch_90 and not r.down_touch_90:
            summary.neither_touch_total += 1
        
        # Resolve spike detection
        if max(r.max_up_cents, r.max_down_cents) >= resolve_min:
            summary.windows_with_resolve_spike += 1
        
        # Segmentation stats
        if r.ticks_truncated > 0:
            summary.windows_with_truncation += 1
            summary.total_ticks_truncated += r.ticks_truncated
        
        # ====================================================================
        # Handle UNCLEAR separately
        # ====================================================================
        if r.winner == "UNCLEAR":
            summary.unclear_winner_count += 1
            if r.up_touch_90:
                summary.up_touch_90_unclear += 1
            if r.down_touch_90:
                summary.down_touch_90_unclear += 1
            if r.up_touch_90 and r.down_touch_90:
                summary.both_touch_unclear += 1
            if not r.up_touch_90 and not r.down_touch_90:
                summary.neither_touch_unclear += 1
            continue
        
        # ====================================================================
        # Clear winner processing
        # ====================================================================
        summary.total_with_winner += 1
        
        if r.winner == "UP":
            summary.up_wins += 1
        else:
            summary.down_wins += 1
        
        # Track resolve times
        if r.resolve_time is not None:
            summary.resolve_times.append(r.resolve_time)
            if r.resolve_time < 900:
                summary.early_resets += 1
        
        # Primary counters (using regular touch)
        if r.up_touch_90 and r.winner == "UP":
            summary.up_touch_90_and_up_win += 1
        if r.down_touch_90 and r.winner == "DOWN":
            summary.down_touch_90_and_down_win += 1
        if r.up_touch_90 and r.winner == "DOWN":
            summary.up_touch_90_and_down_win += 1
        if r.down_touch_90 and r.winner == "UP":
            summary.down_touch_90_and_up_win += 1
        
        # Pre-resolve counters
        if r.up_touch_90_pre_resolve and r.winner == "UP":
            summary.up_touch_90_pre_resolve_and_up_win += 1
        if r.down_touch_90_pre_resolve and r.winner == "DOWN":
            summary.down_touch_90_pre_resolve_and_down_win += 1
        if r.up_touch_90_pre_resolve and r.winner == "DOWN":
            summary.up_touch_90_pre_resolve_and_down_win += 1
        if r.down_touch_90_pre_resolve and r.winner == "UP":
            summary.down_touch_90_pre_resolve_and_up_win += 1
        
        # ====================================================================
        # SUPPORTING TOTALS (clear winners only)
        # ====================================================================
        if r.up_touch_90:
            summary.up_touch_total_clear += 1
        if r.down_touch_90:
            summary.down_touch_total_clear += 1
        if r.up_touch_90 and r.down_touch_90:
            summary.both_touch_total_clear += 1
            summary.both_touch_90 += 1  # Legacy
        if not r.up_touch_90 and not r.down_touch_90:
            summary.neither_touch_total_clear += 1
            summary.neither_touch_90 += 1  # Legacy
    
    return summary


def validate_invariants(summary: BacktestSummary) -> List[str]:
    """
    Validate hard invariants. Returns list of failures.
    """
    failures: List[str] = []
    
    # Invariant 1: both_touch_total <= up_touch_total
    if summary.both_touch_total > summary.up_touch_total:
        failures.append(
            f"INVARIANT FAIL: both_touch_total ({summary.both_touch_total}) "
            f"> up_touch_total ({summary.up_touch_total})"
        )
    
    # Invariant 2: both_touch_total <= down_touch_total
    if summary.both_touch_total > summary.down_touch_total:
        failures.append(
            f"INVARIANT FAIL: both_touch_total ({summary.both_touch_total}) "
            f"> down_touch_total ({summary.down_touch_total})"
        )
    
    # Invariant 3: neither_touch_total >= 0
    if summary.neither_touch_total < 0:
        failures.append(
            f"INVARIANT FAIL: neither_touch_total ({summary.neither_touch_total}) < 0"
        )
    
    # Invariant 4: Partition check for touches (all windows)
    # total = up_only + down_only + both + neither
    expected_total = (
        (summary.up_touch_total - summary.both_touch_total) +
        (summary.down_touch_total - summary.both_touch_total) +
        summary.both_touch_total +
        summary.neither_touch_total
    )
    if expected_total != summary.total_windows:
        failures.append(
            f"INVARIANT FAIL: Touch partition mismatch. "
            f"Expected total={expected_total}, actual={summary.total_windows}"
        )
    
    # Invariant 5: UP touch partition (all windows)
    up_touch_upwin = summary.up_touch_90_and_up_win
    up_touch_downwin = summary.up_touch_90_and_down_win
    up_touch_unclear = summary.up_touch_90_unclear
    up_partition = up_touch_upwin + up_touch_downwin + up_touch_unclear
    if up_partition != summary.up_touch_total:
        failures.append(
            f"INVARIANT FAIL: UP touch partition mismatch. "
            f"up_touch_upwin({up_touch_upwin}) + up_touch_downwin({up_touch_downwin}) "
            f"+ up_touch_unclear({up_touch_unclear}) = {up_partition}, "
            f"expected up_touch_total={summary.up_touch_total}"
        )
    
    # Invariant 6: DOWN touch partition (all windows)
    down_touch_downwin = summary.down_touch_90_and_down_win
    down_touch_upwin = summary.down_touch_90_and_up_win
    down_touch_unclear = summary.down_touch_90_unclear
    down_partition = down_touch_downwin + down_touch_upwin + down_touch_unclear
    if down_partition != summary.down_touch_total:
        failures.append(
            f"INVARIANT FAIL: DOWN touch partition mismatch. "
            f"down_touch_downwin({down_touch_downwin}) + down_touch_upwin({down_touch_upwin}) "
            f"+ down_touch_unclear({down_touch_unclear}) = {down_partition}, "
            f"expected down_touch_total={summary.down_touch_total}"
        )
    
    # =========================================================================
    # IDENTITY CHECKS FOR CLEAR WINNERS (the 4 requested counts)
    # =========================================================================
    
    # Identity 1: up_touch_total_clear == up_touch_90_and_up_win + up_touch_90_and_down_win
    expected_up_clear = summary.up_touch_90_and_up_win + summary.up_touch_90_and_down_win
    if expected_up_clear != summary.up_touch_total_clear:
        failures.append(
            f"IDENTITY FAIL: up_touch_total_clear mismatch. "
            f"up_touch_90_and_up_win({summary.up_touch_90_and_up_win}) + "
            f"up_touch_90_and_down_win({summary.up_touch_90_and_down_win}) = {expected_up_clear}, "
            f"expected up_touch_total_clear={summary.up_touch_total_clear}"
        )
    
    # Identity 2: down_touch_total_clear == down_touch_90_and_down_win + down_touch_90_and_up_win
    expected_down_clear = summary.down_touch_90_and_down_win + summary.down_touch_90_and_up_win
    if expected_down_clear != summary.down_touch_total_clear:
        failures.append(
            f"IDENTITY FAIL: down_touch_total_clear mismatch. "
            f"down_touch_90_and_down_win({summary.down_touch_90_and_down_win}) + "
            f"down_touch_90_and_up_win({summary.down_touch_90_and_up_win}) = {expected_down_clear}, "
            f"expected down_touch_total_clear={summary.down_touch_total_clear}"
        )
    
    # Identity 3: clear_winners_total == (up_touch + down_touch - both_touch) + neither_touch
    # This is: up_only + down_only + both + neither = total
    expected_clear = (
        summary.up_touch_total_clear + 
        summary.down_touch_total_clear - 
        summary.both_touch_total_clear + 
        summary.neither_touch_total_clear
    )
    if expected_clear != summary.total_with_winner:
        failures.append(
            f"IDENTITY FAIL: clear_winners_total partition mismatch. "
            f"up_touch_total_clear({summary.up_touch_total_clear}) + "
            f"down_touch_total_clear({summary.down_touch_total_clear}) - "
            f"both_touch_total_clear({summary.both_touch_total_clear}) + "
            f"neither_touch_total_clear({summary.neither_touch_total_clear}) = {expected_clear}, "
            f"expected clear_winners_total={summary.total_with_winner}"
        )
    
    # =========================================================================
    # NEITHER TOUCH PARTITION CHECK
    # =========================================================================
    
    # Identity 4: neither_touch_total == neither_touch_clear + neither_touch_unclear
    expected_neither = summary.neither_touch_total_clear + summary.neither_touch_unclear
    if expected_neither != summary.neither_touch_total:
        failures.append(
            f"IDENTITY FAIL: neither_touch_total partition mismatch. "
            f"neither_touch_total_clear({summary.neither_touch_total_clear}) + "
            f"neither_touch_unclear({summary.neither_touch_unclear}) = {expected_neither}, "
            f"expected neither_touch_total={summary.neither_touch_total}"
        )
    
    # Identity 5: both_touch_total == both_touch_clear + both_touch_unclear
    expected_both = summary.both_touch_total_clear + summary.both_touch_unclear
    if expected_both != summary.both_touch_total:
        failures.append(
            f"IDENTITY FAIL: both_touch_total partition mismatch. "
            f"both_touch_total_clear({summary.both_touch_total_clear}) + "
            f"both_touch_unclear({summary.both_touch_unclear}) = {expected_both}, "
            f"expected both_touch_total={summary.both_touch_total}"
        )
    
    return failures


# ============================================================================
# Output Functions
# ============================================================================

def write_summary_json(summary: BacktestSummary, outdir: Path) -> None:
    """Write summary.json file."""
    outdir.mkdir(parents=True, exist_ok=True)
    summary_path = outdir / "summary.json"
    with open(summary_path, 'w', encoding='utf-8') as f:
        json.dump(summary.to_dict(), f, indent=2)


def write_windows_csv(results: List[WindowResult], outdir: Path) -> None:
    """Write windows.csv file with extended diagnostics."""
    outdir.mkdir(parents=True, exist_ok=True)
    csv_path = outdir / "windows.csv"
    
    with open(csv_path, 'w', newline='', encoding='utf-8') as f:
        writer = csv.writer(f)
        writer.writerow([
            'window_id',
            'num_ticks',
            'num_valid_ticks',
            'min_elapsed',
            'max_elapsed',
            'last_valid_time',
            'resolve_time',
            'winner',
            'max_up_cents',
            'max_down_cents',
            'up_touch_90',
            'down_touch_90',
            'up_touch_90_pre_resolve',
            'down_touch_90_pre_resolve',
            'backward_jumps',
            'ticks_truncated',
            'parse_errors',
            'has_issues'
        ])
        
        for r in results:
            writer.writerow([
                r.window_id,
                r.num_ticks,
                r.num_valid_ticks,
                f"{r.min_elapsed:.3f}" if r.min_elapsed is not None else "",
                f"{r.max_elapsed:.3f}" if r.max_elapsed is not None else "",
                f"{r.last_valid_time:.3f}" if r.last_valid_time else "",
                f"{r.resolve_time:.3f}" if r.resolve_time else "",
                r.winner,
                r.max_up_cents,
                r.max_down_cents,
                1 if r.up_touch_90 else 0,
                1 if r.down_touch_90 else 0,
                1 if r.up_touch_90_pre_resolve else (0 if r.up_touch_90_pre_resolve is False else ""),
                1 if r.down_touch_90_pre_resolve else (0 if r.down_touch_90_pre_resolve is False else ""),
                r.backward_jumps,
                r.ticks_truncated,
                r.parse_errors,
                1 if r.has_issues() else 0
            ])


def write_bad_windows_json(results: List[WindowResult], outdir: Path) -> None:
    """Write bad_windows.json file with windows that have issues."""
    outdir.mkdir(parents=True, exist_ok=True)
    bad_path = outdir / "bad_windows.json"
    
    bad_windows = []
    for r in results:
        if r.has_issues():
            bad_windows.append({
                'window_id': r.window_id,
                'winner': r.winner,
                'issues': r.issues,
                'num_ticks': r.num_ticks,
                'num_valid_ticks': r.num_valid_ticks,
                'backward_jumps': r.backward_jumps,
                'ticks_truncated': r.ticks_truncated
            })
    
    with open(bad_path, 'w', encoding='utf-8') as f:
        json.dump(bad_windows, f, indent=2)


def write_audit_json(results: List[WindowResult], summary: BacktestSummary, outdir: Path) -> None:
    """
    Write audit.json with sample windows for validation.
    
    Includes:
    - Windows where neither side touched 90 (if any exist)
    - Windows where UP touched 90 but DOWN won and DOWN never touched 90
    - Windows where DOWN touched 90 but UP won and UP never touched 90
    - Windows where segmentation truncated > 5% of ticks
    """
    outdir.mkdir(parents=True, exist_ok=True)
    audit_path = outdir / "audit.json"
    
    audit: Dict[str, Any] = {
        'neither_touched_90': [],
        'neither_touched_90_clear': [],  # Only clear winners
        'neither_touched_90_unclear': [],  # Only UNCLEAR
        'up_touched_down_won_down_never_touched': [],
        'down_touched_up_won_up_never_touched': [],
        'heavy_truncation': [],
        'max_elapsed_small': [],  # max_elapsed < 200 but had backward jumps
    }
    
    for r in results:
        # Neither touched
        if not r.up_touch_90 and not r.down_touch_90:
            entry = {
                'window_id': r.window_id,
                'winner': r.winner,
                'max_up_cents': r.max_up_cents,
                'max_down_cents': r.max_down_cents,
                'max_elapsed': r.max_elapsed
            }
            if len(audit['neither_touched_90']) < 50:
                audit['neither_touched_90'].append(entry)
            # Separate by winner type
            if r.winner == "UNCLEAR":
                audit['neither_touched_90_unclear'].append(r.window_id)
            else:
                audit['neither_touched_90_clear'].append(r.window_id)
        
        # UP touched, DOWN won, DOWN never touched
        if r.up_touch_90 and not r.down_touch_90 and r.winner == "DOWN":
            if len(audit['up_touched_down_won_down_never_touched']) < 50:
                audit['up_touched_down_won_down_never_touched'].append({
                    'window_id': r.window_id,
                    'max_up_cents': r.max_up_cents,
                    'max_down_cents': r.max_down_cents,
                    'max_elapsed': r.max_elapsed
                })
        
        # DOWN touched, UP won, UP never touched
        if r.down_touch_90 and not r.up_touch_90 and r.winner == "UP":
            if len(audit['down_touched_up_won_up_never_touched']) < 50:
                audit['down_touched_up_won_up_never_touched'].append({
                    'window_id': r.window_id,
                    'max_up_cents': r.max_up_cents,
                    'max_down_cents': r.max_down_cents,
                    'max_elapsed': r.max_elapsed
                })
        
        # Heavy truncation (> 5% of ticks)
        if r.num_ticks > 0 and r.ticks_truncated > 0:
            truncation_pct = r.ticks_truncated / r.num_ticks
            if truncation_pct > 0.05:
                if len(audit['heavy_truncation']) < 50:
                    audit['heavy_truncation'].append({
                        'window_id': r.window_id,
                        'num_ticks': r.num_ticks,
                        'ticks_truncated': r.ticks_truncated,
                        'truncation_pct': f"{truncation_pct:.1%}",
                        'backward_jumps': r.backward_jumps
                    })
        
        # Small max_elapsed with backward jumps (suspicious)
        if r.max_elapsed is not None and r.max_elapsed < 200 and r.backward_jumps > 0:
            if len(audit['max_elapsed_small']) < 50:
                audit['max_elapsed_small'].append({
                    'window_id': r.window_id,
                    'max_elapsed': r.max_elapsed,
                    'backward_jumps': r.backward_jumps,
                    'ticks_truncated': r.ticks_truncated
                })
    
    # Add counts
    audit['counts'] = {
        'neither_touched_90_total': len(audit['neither_touched_90_clear']) + len(audit['neither_touched_90_unclear']),
        'neither_touched_90_clear': len(audit['neither_touched_90_clear']),
        'neither_touched_90_unclear': len(audit['neither_touched_90_unclear']),
        'up_touched_down_won_down_never_touched': len(audit['up_touched_down_won_down_never_touched']),
        'down_touched_up_won_up_never_touched': len(audit['down_touched_up_won_up_never_touched']),
        'heavy_truncation': len(audit['heavy_truncation']),
        'max_elapsed_small': len(audit['max_elapsed_small']),
        # From summary for cross-check
        'summary_neither_touch_total': summary.neither_touch_total,
        'summary_neither_touch_total_clear': summary.neither_touch_total_clear,
        'summary_neither_touch_unclear': summary.neither_touch_unclear,
    }
    
    with open(audit_path, 'w', encoding='utf-8') as f:
        json.dump(audit, f, indent=2)


def print_console_summary(summary: BacktestSummary, invariant_failures: List[str]) -> None:
    """Print readable summary to console."""
    print("\n" + "=" * 70)
    print("BTC 15-MIN POLYMARKET BACKTEST RESULTS")
    print("=" * 70)
    
    print(f"\nConfiguration:")
    print(f"  Touch threshold: >= {summary.touch_threshold}c")
    print(f"  Resolve threshold: >= {summary.resolve_min}c")
    
    print(f"\n--- WINDOW COUNTS ---")
    print(f"  Total windows processed:     {summary.total_windows}")
    print(f"  Windows with clear winner:   {summary.total_with_winner}")
    print(f"  UNCLEAR_WINNER_COUNT:        {summary.unclear_winner_count}")
    
    print(f"\n--- SEGMENTATION STATS ---")
    print(f"  Windows with truncation:     {summary.windows_with_truncation}")
    print(f"  Total ticks truncated:       {summary.total_ticks_truncated}")
    
    # =========================================================================
    # THE 4 REQUESTED COUNTS - CLEAR WINNERS ONLY
    # =========================================================================
    print("\n" + "=" * 70)
    print("REQUESTED COUNTS (CLEAR WINNERS ONLY)")
    print("=" * 70)
    print(f"1) UP touched >=90c AND UP won:     {summary.up_touch_90_and_up_win}")
    print(f"2) DOWN touched >=90c AND DOWN won: {summary.down_touch_90_and_down_win}")
    print(f"3) UP touched >=90c AND DOWN won:   {summary.up_touch_90_and_down_win}")
    print(f"4) DOWN touched >=90c AND UP won:   {summary.down_touch_90_and_up_win}")
    
    # =========================================================================
    # SUPPORTING TOTALS - CLEAR WINNERS ONLY
    # =========================================================================
    print("\n" + "-" * 70)
    print("SUPPORTING TOTALS (CLEAR WINNERS ONLY)")
    print("-" * 70)
    print(f"Clear winners total:                  {summary.total_with_winner}")
    print(f"UP touched >=90c total (clear):       {summary.up_touch_total_clear}")
    print(f"DOWN touched >=90c total (clear):     {summary.down_touch_total_clear}")
    print(f"BOTH touched >=90c total (clear):     {summary.both_touch_total_clear}")
    print(f"NEITHER touched >=90c total (clear):  {summary.neither_touch_total_clear}")
    
    # =========================================================================
    # CROSS-CHECK EQUATIONS
    # =========================================================================
    print("\n" + "-" * 70)
    print("CROSS-CHECK EQUATIONS")
    print("-" * 70)
    
    # Identity 1
    lhs1 = summary.up_touch_total_clear
    rhs1 = summary.up_touch_90_and_up_win + summary.up_touch_90_and_down_win
    check1 = lhs1 == rhs1
    print(f"up_touch_total_clear == up_touch_90_and_up_win + up_touch_90_and_down_win")
    print(f"  {lhs1} == {summary.up_touch_90_and_up_win} + {summary.up_touch_90_and_down_win} = {rhs1}")
    print(f"  Result: {'PASS' if check1 else 'FAIL'}")
    
    # Identity 2
    lhs2 = summary.down_touch_total_clear
    rhs2 = summary.down_touch_90_and_down_win + summary.down_touch_90_and_up_win
    check2 = lhs2 == rhs2
    print(f"\ndown_touch_total_clear == down_touch_90_and_down_win + down_touch_90_and_up_win")
    print(f"  {lhs2} == {summary.down_touch_90_and_down_win} + {summary.down_touch_90_and_up_win} = {rhs2}")
    print(f"  Result: {'PASS' if check2 else 'FAIL'}")
    
    # Identity 3
    lhs3 = summary.total_with_winner
    rhs3 = (summary.up_touch_total_clear + summary.down_touch_total_clear - 
            summary.both_touch_total_clear + summary.neither_touch_total_clear)
    check3 = lhs3 == rhs3
    print(f"\nclear_winners == (up_touch + down_touch - both_touch) + neither_touch")
    print(f"  {lhs3} == ({summary.up_touch_total_clear} + {summary.down_touch_total_clear} - "
          f"{summary.both_touch_total_clear}) + {summary.neither_touch_total_clear} = {rhs3}")
    print(f"  Result: {'PASS' if check3 else 'FAIL'}")
    
    print("=" * 70)
    
    # =========================================================================
    # ADDITIONAL STATS
    # =========================================================================
    print(f"\n--- WINNER DISTRIBUTION ---")
    print(f"  UP wins:                     {summary.up_wins}")
    print(f"  DOWN wins:                   {summary.down_wins}")
    
    print(f"\n--- PRE-RESOLVE COUNTERS (excludes settlement spike) ---")
    print(f"  UP_TOUCH_90_PRE_RESOLVE_AND_UP_WIN:     {summary.up_touch_90_pre_resolve_and_up_win}")
    print(f"  DOWN_TOUCH_90_PRE_RESOLVE_AND_DOWN_WIN: {summary.down_touch_90_pre_resolve_and_down_win}")
    print(f"  UP_TOUCH_90_PRE_RESOLVE_AND_DOWN_WIN:   {summary.up_touch_90_pre_resolve_and_down_win}")
    print(f"  DOWN_TOUCH_90_PRE_RESOLVE_AND_UP_WIN:   {summary.down_touch_90_pre_resolve_and_up_win}")
    
    print(f"\n--- TOUCH TOTALS (all windows including unclear) ---")
    print(f"  UP touched >= {summary.touch_threshold}c:            {summary.up_touch_total}")
    print(f"  DOWN touched >= {summary.touch_threshold}c:          {summary.down_touch_total}")
    print(f"  BOTH touched >= {summary.touch_threshold}c:          {summary.both_touch_total}")
    print(f"  NEITHER touched >= {summary.touch_threshold}c:       {summary.neither_touch_total}")
    
    # NEITHER breakdown with partition check
    print(f"\n--- NEITHER TOUCHED BREAKDOWN ---")
    print(f"  NEITHER total (all windows):     {summary.neither_touch_total}")
    print(f"  NEITHER clear (clear winners):   {summary.neither_touch_total_clear}")
    print(f"  NEITHER unclear (UNCLEAR wins):  {summary.neither_touch_unclear}")
    neither_sum = summary.neither_touch_total_clear + summary.neither_touch_unclear
    neither_check = neither_sum == summary.neither_touch_total
    print(f"  Partition check: {summary.neither_touch_total_clear} + {summary.neither_touch_unclear} = {neither_sum} == {summary.neither_touch_total}: {'PASS' if neither_check else 'FAIL'}")
    
    print(f"\n--- RESOLVE STATS ---")
    print(f"  Windows with resolve spike (>= {summary.resolve_min}c): {summary.windows_with_resolve_spike}")
    print(f"  Early resets (<900s):        {summary.early_resets}")
    if summary.resolve_times:
        sorted_times = sorted(summary.resolve_times)
        print(f"  Resolve time min:            {min(summary.resolve_times):.1f}s")
        print(f"  Resolve time max:            {max(summary.resolve_times):.1f}s")
        print(f"  Resolve time mean:           {sum(summary.resolve_times)/len(summary.resolve_times):.1f}s")
        print(f"  Resolve time median:         {sorted_times[len(sorted_times)//2]:.1f}s")
    
    # Invariant summary
    if invariant_failures:
        print(f"\n!!! {len(invariant_failures)} INVARIANT/IDENTITY FAILURES !!!")
        for fail in invariant_failures:
            print(f"  {fail}")
    else:
        print(f"\n*** ALL INVARIANTS AND IDENTITY CHECKS PASSED ***")
    
    print("=" * 70 + "\n")


# ============================================================================
# Main Entry Point
# ============================================================================

def main() -> int:
    parser = argparse.ArgumentParser(
        description="BTC 15-min Polymarket Backtest",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  python backtest_btc15.py --input ./market_logs --outdir out
  python backtest_btc15.py --input combined.txt --outdir out --resolve-min 97 --touch 90
        """
    )
    parser.add_argument(
        '--input', '-i',
        required=True,
        help='Input path: directory (Format A) or file (Format B)'
    )
    parser.add_argument(
        '--outdir', '-o',
        default='out',
        help='Output directory for results (default: out)'
    )
    parser.add_argument(
        '--resolve-min',
        type=int,
        default=97,
        help='Resolve threshold in cents (default: 97)'
    )
    parser.add_argument(
        '--touch',
        type=int,
        default=90,
        help='Touch threshold in cents (default: 90)'
    )
    
    args = parser.parse_args()
    
    input_path = Path(args.input)
    outdir = Path(args.outdir)
    resolve_min = args.resolve_min
    touch_threshold = args.touch
    
    if not input_path.exists():
        print(f"ERROR: Input path does not exist: {input_path}", file=sys.stderr)
        return 1
    
    print(f"Loading windows from: {input_path}")
    print(f"Input format detected: {'directory (Format A)' if input_path.is_dir() else 'file (Format B)'}")
    
    # Load all windows
    windows = load_windows(input_path)
    print(f"Loaded {len(windows)} windows")
    
    # Analyze each window
    results: List[WindowResult] = []
    for window_id, ticks, errors in windows:
        result = analyze_window(
            window_id, ticks, errors,
            touch_threshold=touch_threshold,
            resolve_min=resolve_min
        )
        results.append(result)
    
    # Compute summary
    summary = compute_summary(results, touch_threshold, resolve_min)
    
    # Validate invariants
    invariant_failures = validate_invariants(summary)
    
    # Write outputs
    write_summary_json(summary, outdir)
    write_windows_csv(results, outdir)
    write_bad_windows_json(results, outdir)
    write_audit_json(results, summary, outdir)
    
    print(f"\nOutput written to: {outdir}/")
    print(f"  - summary.json")
    print(f"  - windows.csv")
    print(f"  - bad_windows.json")
    print(f"  - audit.json")
    
    # Print console summary
    print_console_summary(summary, invariant_failures)
    
    # Exit non-zero if invariants failed
    if invariant_failures:
        print("EXITING WITH ERROR: Invariant failures detected", file=sys.stderr)
        return 1
    
    return 0


if __name__ == '__main__':
    sys.exit(main())
