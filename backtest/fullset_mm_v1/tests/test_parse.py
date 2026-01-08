"""Tests for parse.py - tick parsing with safety guards."""
import pytest
import sys
import os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(__file__))))

from fullset_mm_v1.parse import parse_tick_line, RawTick


class TestTickParsing:
    """Test tick line parsing."""
    
    def test_parse_valid_tick(self):
        line = "00:00:061 - UP 46C | DOWN 60C"
        tick = parse_tick_line(line)
        
        assert tick is not None
        assert tick.is_valid
        assert abs(tick.elapsed_secs - 0.061) < 0.001
        assert tick.up_cents == 46
        assert tick.down_cents == 60
    
    def test_parse_tick_with_minutes(self):
        line = "05:30:500 - UP 90C | DOWN 12C"
        tick = parse_tick_line(line)
        
        assert tick is not None
        assert abs(tick.elapsed_secs - (5*60 + 30 + 0.5)) < 0.001
        assert tick.up_cents == 90
        assert tick.down_cents == 12
    
    def test_parse_invalid_tick_negative_up(self):
        line = "12:44:872 - UP -1C | DOWN 1C"
        tick = parse_tick_line(line)
        
        assert tick is not None
        assert not tick.is_valid  # Invalid because UP = -1
        assert tick.up_cents == -1
    
    def test_parse_invalid_tick_negative_down(self):
        line = "12:44:872 - UP 99C | DOWN -1C"
        tick = parse_tick_line(line)
        
        assert tick is not None
        assert not tick.is_valid  # Invalid because DOWN = -1
    
    def test_parse_non_matching_line(self):
        line = "some random text"
        tick = parse_tick_line(line)
        
        assert tick is None
    
    def test_parse_empty_line(self):
        line = ""
        tick = parse_tick_line(line)
        
        assert tick is None
    
    def test_parse_high_value_ticks(self):
        line = "10:00:000 - UP 99C | DOWN 2C"
        tick = parse_tick_line(line)
        
        assert tick is not None
        assert tick.is_valid
        assert tick.up_cents == 99
        assert tick.down_cents == 2


class TestParseStopConditions:
    """Test that parsing stops at appropriate conditions."""
    
    def test_stop_on_invalid_tick(self):
        """Parsing should stop when we hit an invalid tick."""
        from fullset_mm_v1.parse import parse_tick_file
        import tempfile
        
        # Create temp file with valid ticks then invalid
        content = """00:00:100 - UP 50C | DOWN 50C
00:01:000 - UP 55C | DOWN 45C
00:02:000 - UP -1C | DOWN 99C
00:03:000 - UP 99C | DOWN 1C
"""
        with tempfile.NamedTemporaryFile(mode='w', suffix='.txt', delete=False) as f:
            f.write(content)
            filepath = f.name
        
        try:
            ticks = parse_tick_file(filepath)
            # Should have stopped at the -1 tick
            assert len(ticks) == 2
        finally:
            os.unlink(filepath)
    
    def test_stop_on_elapsed_over_901(self):
        """Parsing should stop when elapsed > 901s."""
        from fullset_mm_v1.parse import parse_tick_file
        import tempfile
        
        content = """14:00:000 - UP 50C | DOWN 50C
15:00:000 - UP 55C | DOWN 45C
15:02:000 - UP 60C | DOWN 40C
"""
        # 14:00 = 840s (OK), 15:00 = 900s (OK), 15:02 = 902s (STOP)
        with tempfile.NamedTemporaryFile(mode='w', suffix='.txt', delete=False) as f:
            f.write(content)
            filepath = f.name
        
        try:
            ticks = parse_tick_file(filepath)
            # Should have stopped before 902s tick
            assert len(ticks) == 2
            assert ticks[-1].elapsed_secs == 900.0
        finally:
            os.unlink(filepath)
    
    def test_stop_on_time_reset(self):
        """Parsing should stop when time goes backwards (contamination)."""
        from fullset_mm_v1.parse import parse_tick_file
        import tempfile
        
        content = """10:00:000 - UP 50C | DOWN 50C
11:00:000 - UP 55C | DOWN 45C
00:01:000 - UP 60C | DOWN 40C
00:02:000 - UP 65C | DOWN 35C
"""
        # Time jumps from 660s back to 1s = contamination
        with tempfile.NamedTemporaryFile(mode='w', suffix='.txt', delete=False) as f:
            f.write(content)
            filepath = f.name
        
        try:
            ticks = parse_tick_file(filepath)
            # Should have stopped at time reset
            assert len(ticks) == 2
        finally:
            os.unlink(filepath)


if __name__ == "__main__":
    pytest.main([__file__, "-v"])


