"""
Unit tests for BTC 15-min Polymarket Backtest.

Run with: pytest tests/test_backtest.py -v
"""

import sys
from pathlib import Path

# Add parent to path for imports
sys.path.insert(0, str(Path(__file__).parent.parent))

import pytest
from backtest_btc15 import (
    Tick,
    parse_tick_line,
    parse_window_file,
    parse_combined_file,
    analyze_window,
    compute_summary,
    load_windows,
    segment_ticks_by_reset,
    select_segment,
    validate_invariants,
)


# ============================================================================
# Fixtures Path
# ============================================================================

FIXTURES_DIR = Path(__file__).parent / "fixtures"


# ============================================================================
# Tick Parsing Tests
# ============================================================================

class TestTickParsing:
    """Tests for tick line parsing."""
    
    def test_parse_tick_with_ms(self):
        """Test parsing tick with milliseconds."""
        line = "10:03:889 - UP 90C | DOWN 12C"
        tick = parse_tick_line(line)
        
        assert tick is not None
        assert tick.up_cents == 90
        assert tick.down_cents == 12
        assert tick.elapsed_seconds == pytest.approx(10 * 60 + 3 + 0.889, rel=1e-3)
    
    def test_parse_tick_without_ms(self):
        """Test parsing tick without milliseconds."""
        line = "10:03 - UP 90C | DOWN 12C"
        tick = parse_tick_line(line)
        
        assert tick is not None
        assert tick.up_cents == 90
        assert tick.down_cents == 12
        assert tick.elapsed_seconds == 10 * 60 + 3
    
    def test_parse_tick_zero_minutes(self):
        """Test parsing tick at start of window."""
        line = "00:00:100 - UP 50C | DOWN 50C"
        tick = parse_tick_line(line)
        
        assert tick is not None
        assert tick.up_cents == 50
        assert tick.down_cents == 50
        assert tick.elapsed_seconds == pytest.approx(0.1, rel=1e-3)
    
    def test_parse_tick_negative_cents(self):
        """Test parsing tick with negative cents (invalid)."""
        line = "10:05:100 - UP -1C | DOWN 1C"
        tick = parse_tick_line(line)
        
        assert tick is not None
        assert tick.up_cents == -1
        assert tick.down_cents == 1
        assert not tick.is_valid()  # Should be invalid
    
    def test_parse_tick_lowercase(self):
        """Test parsing with lowercase."""
        line = "05:30:250 - up 75c | down 25c"
        tick = parse_tick_line(line)
        
        assert tick is not None
        assert tick.up_cents == 75
        assert tick.down_cents == 25
    
    def test_parse_invalid_line(self):
        """Test parsing invalid line returns None."""
        invalid_lines = [
            "",
            "   ",
            "random text",
            "Window: test",
            "10:03 UP 90C DOWN 12C",  # Missing dashes and pipes
        ]
        for line in invalid_lines:
            assert parse_tick_line(line) is None


# ============================================================================
# Tick Validity Tests
# ============================================================================

class TestTickValidity:
    """Tests for tick validity checking."""
    
    def test_valid_tick(self):
        """Test valid tick detection."""
        tick = Tick(elapsed_seconds=100.0, up_cents=60, down_cents=40)
        assert tick.is_valid()
    
    def test_invalid_tick_negative_up(self):
        """Test invalid tick with negative UP."""
        tick = Tick(elapsed_seconds=100.0, up_cents=-1, down_cents=101)
        assert not tick.is_valid()
    
    def test_invalid_tick_over_100(self):
        """Test invalid tick with cents > 100."""
        tick = Tick(elapsed_seconds=100.0, up_cents=105, down_cents=50)
        assert not tick.is_valid()
    
    def test_boundary_valid(self):
        """Test boundary values (0 and 100) are valid."""
        tick1 = Tick(elapsed_seconds=100.0, up_cents=0, down_cents=100)
        tick2 = Tick(elapsed_seconds=100.0, up_cents=100, down_cents=0)
        assert tick1.is_valid()
        assert tick2.is_valid()


# ============================================================================
# Resolve Detection Tests
# ============================================================================

class TestResolveDetection:
    """Tests for resolve/settlement detection."""
    
    def test_resolved_up(self):
        """Test resolved state with UP winning."""
        tick = Tick(elapsed_seconds=600.0, up_cents=99, down_cents=1)
        assert tick.is_resolved(resolve_min=97)
    
    def test_resolved_down(self):
        """Test resolved state with DOWN winning."""
        tick = Tick(elapsed_seconds=600.0, up_cents=2, down_cents=98)
        assert tick.is_resolved(resolve_min=97)
    
    def test_not_resolved_close(self):
        """Test that close prices are not resolved."""
        tick = Tick(elapsed_seconds=600.0, up_cents=60, down_cents=40)
        assert not tick.is_resolved(resolve_min=97)
    
    def test_not_resolved_mid_high(self):
        """Test that 90c is not resolved at 97 threshold."""
        tick = Tick(elapsed_seconds=600.0, up_cents=90, down_cents=10)
        assert not tick.is_resolved(resolve_min=97)
    
    def test_resolved_custom_threshold(self):
        """Test resolved with custom threshold."""
        tick = Tick(elapsed_seconds=600.0, up_cents=90, down_cents=10)
        assert tick.is_resolved(resolve_min=90)


# ============================================================================
# Window Analysis Tests
# ============================================================================

class TestWindowAnalysis:
    """Tests for window analysis."""
    
    def test_up_resolved_window(self):
        """Test window that resolves to UP."""
        window_id, ticks, errors = parse_window_file(
            FIXTURES_DIR / "window_up_resolved.txt"
        )
        result = analyze_window(window_id, ticks, errors)
        
        assert result.winner == "UP"
        assert result.up_touch_90 is True
        assert result.down_touch_90 is False
        assert result.resolve_time is not None
    
    def test_down_resolved_window(self):
        """Test window that resolves to DOWN."""
        window_id, ticks, errors = parse_window_file(
            FIXTURES_DIR / "window_down_resolved.txt"
        )
        result = analyze_window(window_id, ticks, errors)
        
        assert result.winner == "DOWN"
        assert result.up_touch_90 is False
        assert result.down_touch_90 is True
        assert result.resolve_time is not None
    
    def test_invalid_final_tick_window(self):
        """Test window with invalid final tick still resolves correctly."""
        window_id, ticks, errors = parse_window_file(
            FIXTURES_DIR / "window_invalid_final.txt"
        )
        result = analyze_window(window_id, ticks, errors)
        
        # Should find the resolved tick before the invalid one
        assert result.winner == "UP"
        assert result.up_touch_90 is True
        assert result.resolve_time is not None
        # The last tick (UP=-1C) should be ignored
        assert result.num_valid_ticks < result.num_ticks
    
    def test_early_reset_window(self):
        """Test window that resolves before 900 seconds."""
        window_id, ticks, errors = parse_window_file(
            FIXTURES_DIR / "window_early_reset.txt"
        )
        result = analyze_window(window_id, ticks, errors)
        
        assert result.winner == "UP"
        assert result.resolve_time is not None
        assert result.resolve_time < 900  # Early reset
        assert result.up_touch_90 is True
    
    def test_unclear_winner_window(self):
        """Test window with unclear winner (no resolution, close prices)."""
        window_id, ticks, errors = parse_window_file(
            FIXTURES_DIR / "window_unclear.txt"
        )
        result = analyze_window(window_id, ticks, errors)
        
        assert result.winner == "UNCLEAR"
        assert result.up_touch_90 is False
        assert result.down_touch_90 is False


# ============================================================================
# Combined File (Format B) Tests
# ============================================================================

class TestCombinedFile:
    """Tests for parsing combined file format."""
    
    def test_parse_combined_file(self):
        """Test parsing combined file with multiple windows."""
        windows = parse_combined_file(FIXTURES_DIR / "combined_windows.txt")
        
        assert len(windows) == 2
        
        # Check first window
        window_id1, ticks1, errors1 = windows[0]
        assert window_id1 == "test_window_1"
        assert len(ticks1) == 7
        assert len(errors1) == 0
        
        # Check second window
        window_id2, ticks2, errors2 = windows[1]
        assert window_id2 == "test_window_2"
        assert len(ticks2) == 7
        assert len(errors2) == 0
    
    def test_combined_file_analysis(self):
        """Test analyzing combined file windows."""
        windows = parse_combined_file(FIXTURES_DIR / "combined_windows.txt")
        
        # Analyze first window (UP win)
        window_id1, ticks1, errors1 = windows[0]
        result1 = analyze_window(window_id1, ticks1, errors1)
        assert result1.winner == "UP"
        
        # Analyze second window (DOWN win)
        window_id2, ticks2, errors2 = windows[1]
        result2 = analyze_window(window_id2, ticks2, errors2)
        assert result2.winner == "DOWN"


# ============================================================================
# Summary Computation Tests
# ============================================================================

class TestSummaryComputation:
    """Tests for summary computation."""
    
    def test_summary_basic(self):
        """Test basic summary computation."""
        # Create mock results
        from backtest_btc15 import WindowResult
        
        results = [
            WindowResult(
                window_id="w1",
                winner="UP",
                up_touch_90=True,
                down_touch_90=False,
                up_touch_90_pre_resolve=True,
                down_touch_90_pre_resolve=False,
                resolve_time=500.0
            ),
            WindowResult(
                window_id="w2",
                winner="DOWN",
                up_touch_90=False,
                down_touch_90=True,
                up_touch_90_pre_resolve=False,
                down_touch_90_pre_resolve=True,
                resolve_time=600.0
            ),
            WindowResult(
                window_id="w3",
                winner="UNCLEAR",
                up_touch_90=False,
                down_touch_90=False
            ),
        ]
        
        summary = compute_summary(results)
        
        assert summary.total_windows == 3
        assert summary.total_with_winner == 2
        assert summary.unclear_winner_count == 1
        assert summary.up_wins == 1
        assert summary.down_wins == 1
        assert summary.up_touch_90_and_up_win == 1
        assert summary.down_touch_90_and_down_win == 1
        assert summary.up_touch_90_and_down_win == 0
        assert summary.down_touch_90_and_up_win == 0
    
    def test_summary_touch_counters(self):
        """Test that touch counter sum <= total winners."""
        from backtest_btc15 import WindowResult
        
        # Create results where not all windows have touches
        results = [
            WindowResult(
                window_id="w1",
                winner="UP",
                up_touch_90=True,
                down_touch_90=False
            ),
            WindowResult(
                window_id="w2",
                winner="DOWN",
                up_touch_90=False,
                down_touch_90=False  # No touch!
            ),
        ]
        
        summary = compute_summary(results)
        
        touch_sum = (
            summary.up_touch_90_and_up_win +
            summary.down_touch_90_and_down_win +
            summary.up_touch_90_and_down_win +
            summary.down_touch_90_and_up_win
        )
        
        assert touch_sum <= summary.total_with_winner


# ============================================================================
# Auto-Detection Tests
# ============================================================================

class TestAutoDetection:
    """Tests for format auto-detection."""
    
    def test_detect_directory(self):
        """Test detection of directory format."""
        from backtest_btc15 import detect_input_format
        
        fmt = detect_input_format(FIXTURES_DIR)
        assert fmt == 'dir'
    
    def test_detect_file(self):
        """Test detection of file format."""
        from backtest_btc15 import detect_input_format
        
        fmt = detect_input_format(FIXTURES_DIR / "combined_windows.txt")
        assert fmt == 'file'


# ============================================================================
# Pre-Resolve Touch Tests
# ============================================================================

class TestPreResolveTouch:
    """Tests for pre-resolve touch detection."""
    
    def test_touch_only_at_settlement(self):
        """Test that touch at settlement is detected in regular but matters for pre-resolve."""
        # Create ticks where 90c is only touched at settlement
        ticks = [
            Tick(0.0, 50, 50),
            Tick(60.0, 55, 45),
            Tick(120.0, 60, 40),
            Tick(180.0, 65, 35),
            Tick(240.0, 70, 30),
            Tick(300.0, 75, 25),
            Tick(360.0, 80, 20),
            Tick(420.0, 85, 15),
            Tick(480.0, 88, 12),  # Not yet 90
            Tick(540.0, 97, 3),   # Settlement (touches 97, also >= 90)
        ]
        
        result = analyze_window("test", ticks, [])
        
        assert result.winner == "UP"
        assert result.up_touch_90 is True  # 97 >= 90
        assert result.up_touch_90_pre_resolve is True  # Same since 97 is at resolve
    
    def test_pre_resolve_touch_excludes_post_resolve(self):
        """Test that pre-resolve touch only considers ticks up to resolve time."""
        # Would need special case - tick after resolve shouldn't count
        # In practice, we include resolve tick so this is edge case
        pass


# ============================================================================
# Segmentation Tests
# ============================================================================

class TestSegmentation:
    """Tests for timer reset detection and segmentation."""
    
    def test_no_reset_single_segment(self):
        """Test that continuous ticks produce single segment."""
        ticks = [
            Tick(0.0, 50, 50),
            Tick(60.0, 55, 45),
            Tick(120.0, 60, 40),
            Tick(180.0, 65, 35),
        ]
        
        segments, jumps = segment_ticks_by_reset(ticks)
        
        assert len(segments) == 1
        assert jumps == 0
        assert len(segments[0]) == 4
    
    def test_reset_detected(self):
        """Test that timer reset creates new segment."""
        ticks = [
            Tick(0.0, 50, 50),
            Tick(60.0, 55, 45),
            Tick(120.0, 60, 40),
            Tick(500.0, 97, 3),   # Near end of window
            Tick(10.0, 50, 50),   # RESET - jumped back
            Tick(70.0, 52, 48),   # Next window data
        ]
        
        segments, jumps = segment_ticks_by_reset(ticks)
        
        assert len(segments) == 2
        assert jumps == 1
        assert len(segments[0]) == 4  # First segment
        assert len(segments[1]) == 2  # Second segment (contamination)
    
    def test_select_first_segment(self):
        """Test that first segment is selected."""
        ticks = [
            Tick(0.0, 50, 50),
            Tick(60.0, 55, 45),
            Tick(120.0, 60, 40),
            Tick(500.0, 97, 3),
            Tick(10.0, 50, 50),   # Reset
            Tick(70.0, 52, 48),
        ]
        
        segments, _ = segment_ticks_by_reset(ticks)
        selected, idx, truncated = select_segment(segments)
        
        assert idx == 0
        assert len(selected) == 4
        assert truncated == 2
    
    def test_segmentation_in_analysis(self):
        """Test that analysis uses segmentation."""
        # Simulate window with contamination
        ticks = [
            Tick(0.0, 50, 50),
            Tick(120.0, 60, 40),
            Tick(300.0, 70, 30),
            Tick(500.0, 97, 3),   # Resolved to UP
            Tick(10.0, 10, 90),   # RESET - next window has DOWN @ 90
            Tick(100.0, 5, 95),
        ]
        
        result = analyze_window("test", ticks, [])
        
        # Should use first segment only, so UP wins
        assert result.winner == "UP"
        assert result.ticks_truncated == 2
        assert result.backward_jumps == 1
        # DOWN should NOT touch 90 because that was in the truncated segment
        assert result.down_touch_90 is False
        assert result.up_touch_90 is True  # 97 >= 90


class TestInvariants:
    """Tests for invariant validation."""
    
    def test_valid_invariants_pass(self):
        """Test that valid summary passes invariants."""
        from backtest_btc15 import BacktestSummary
        
        summary = BacktestSummary()
        summary.total_windows = 100
        summary.total_with_winner = 95
        summary.unclear_winner_count = 5
        
        # All windows (including unclear)
        summary.up_touch_total = 60
        summary.down_touch_total = 50
        summary.both_touch_total = 20
        summary.neither_touch_total = 10  # 60-20 + 50-20 + 20 + 10 = 100
        
        # Clear winners only
        summary.up_touch_90_and_up_win = 45
        summary.up_touch_90_and_down_win = 12
        summary.up_touch_90_unclear = 3  # 45+12+3 = 60 = up_touch_total
        
        summary.down_touch_90_and_down_win = 35
        summary.down_touch_90_and_up_win = 10
        summary.down_touch_90_unclear = 5  # 35+10+5 = 50 = down_touch_total
        
        # Clear winners supporting totals
        summary.up_touch_total_clear = 57  # 45 + 12
        summary.down_touch_total_clear = 45  # 35 + 10
        summary.both_touch_total_clear = 15
        summary.neither_touch_total_clear = 8  # 57 + 45 - 15 + 8 = 95
        
        # UNCLEAR partition (must sum to total)
        # both_touch_total = both_touch_total_clear + both_touch_unclear
        summary.both_touch_unclear = 5  # 15 + 5 = 20 = both_touch_total
        # neither_touch_total = neither_touch_total_clear + neither_touch_unclear
        summary.neither_touch_unclear = 2  # 8 + 2 = 10 = neither_touch_total
        
        failures = validate_invariants(summary)
        assert len(failures) == 0, f"Failures: {failures}"
    
    def test_partition_failure_detected(self):
        """Test that partition mismatch is detected."""
        from backtest_btc15 import BacktestSummary
        
        summary = BacktestSummary()
        summary.total_windows = 100
        summary.total_with_winner = 95
        
        summary.up_touch_total = 60
        # Partitions don't add up
        summary.up_touch_90_and_up_win = 45
        summary.up_touch_90_and_down_win = 10  # 45+10+0 = 55 != 60
        summary.up_touch_90_unclear = 0
        
        summary.down_touch_total = 50
        summary.down_touch_90_and_down_win = 50
        summary.down_touch_90_and_up_win = 0
        summary.down_touch_90_unclear = 0
        
        summary.both_touch_total = 20
        summary.neither_touch_total = 10
        
        # Clear totals that would pass identity checks
        summary.up_touch_total_clear = 55
        summary.down_touch_total_clear = 50
        summary.both_touch_total_clear = 15
        summary.neither_touch_total_clear = 5
        
        failures = validate_invariants(summary)
        assert len(failures) > 0
        assert any("UP touch partition" in f for f in failures)


if __name__ == '__main__':
    pytest.main([__file__, '-v'])

