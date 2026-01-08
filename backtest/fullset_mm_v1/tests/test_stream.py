"""Tests for stream.py - tick stream merging."""
import pytest
import sys
import os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(__file__))))

from fullset_mm_v1.stream import merge_tick_streams, QuoteTick
from fullset_mm_v1.parse import RawTick


class TestStreamMerging:
    """Test BUY/SELL stream merging with forward-fill."""
    
    def test_merge_aligned_streams(self):
        """Test merging when both streams have same timestamps."""
        buy_ticks = [
            RawTick(0.0, 50, 50),  # ASK
            RawTick(1.0, 55, 45),
        ]
        sell_ticks = [
            RawTick(0.0, 48, 48),  # BID
            RawTick(1.0, 53, 43),
        ]
        
        result = merge_tick_streams(buy_ticks, sell_ticks)
        
        assert len(result) == 2
        assert result[0].up_ask == 50
        assert result[0].up_bid == 48
        assert result[0].down_ask == 50
        assert result[0].down_bid == 48
    
    def test_merge_with_forward_fill(self):
        """Test forward-fill when streams have different timestamps."""
        # Both streams need to have data before we can emit quotes
        buy_ticks = [
            RawTick(0.0, 50, 50),  # BUY at t=0 (ASK prices)
            RawTick(2.0, 60, 40),  # BUY at t=2
        ]
        sell_ticks = [
            RawTick(0.0, 48, 48),  # SELL at t=0 (BID prices)
            RawTick(1.0, 55, 45),  # SELL at t=1
        ]
        
        result = merge_tick_streams(buy_ticks, sell_ticks)
        
        # t=0: both have data -> should emit (but bid > ask for up so invalid)
        # t=1: buy forward-filled, sell updated
        # t=2: sell forward-filled, buy updated
        
        # Check we have some results
        assert len(result) >= 1
    
    def test_empty_streams(self):
        """Test with empty input streams."""
        result = merge_tick_streams([], [])
        assert result == []
    
    def test_validity_check(self):
        """Test that invalid quotes are filtered out."""
        tick = QuoteTick(
            elapsed_secs=0.0,
            up_ask=50,
            up_bid=55,  # Bid > Ask = invalid!
            down_ask=50,
            down_bid=45
        )
        assert not tick.is_valid()


class TestQuoteTick:
    """Test QuoteTick properties."""
    
    def test_mid_calculation(self):
        tick = QuoteTick(
            elapsed_secs=0.0,
            up_ask=52,
            up_bid=48,
            down_ask=50,
            down_bid=46
        )
        
        assert tick.up_mid == 50.0
        assert tick.down_mid == 48.0
    
    def test_spread_calculation(self):
        tick = QuoteTick(
            elapsed_secs=0.0,
            up_ask=52,
            up_bid=48,
            down_ask=50,
            down_bid=45
        )
        
        assert tick.up_spread == 4
        assert tick.down_spread == 5
    
    def test_validity(self):
        # Valid tick
        valid = QuoteTick(0.0, 52, 48, 50, 45)
        assert valid.is_valid()
        
        # Invalid: bid > ask
        invalid = QuoteTick(0.0, 48, 52, 50, 45)
        assert not invalid.is_valid()
        
        # Invalid: negative price
        invalid2 = QuoteTick(0.0, -1, 48, 50, 45)
        assert not invalid2.is_valid()
        
        # Invalid: over 100
        invalid3 = QuoteTick(0.0, 105, 48, 50, 45)
        assert not invalid3.is_valid()


if __name__ == "__main__":
    pytest.main([__file__, "-v"])

