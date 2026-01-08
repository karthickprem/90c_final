"""Tests for strategy.py - full-set accumulator logic."""
import pytest
import sys
import os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(__file__))))

from fullset_mm_v1.strategy import (
    FullSetAccumulator, compute_maker_bid, check_fill,
    LegState, StrategyState, LegPosition
)
from fullset_mm_v1.stream import QuoteTick, WindowData
from fullset_mm_v1.config import StrategyConfig


class TestMakerBid:
    """Test maker bid computation."""
    
    def test_basic_bid(self):
        # mid=50, d=3 -> bid=47
        bid = compute_maker_bid(50.0, 3)
        assert bid == 47
    
    def test_bid_with_fractional_mid(self):
        # mid=50.5, d=3 -> floor(47.5) = 47
        bid = compute_maker_bid(50.5, 3)
        assert bid == 47
    
    def test_bid_with_zero_offset(self):
        bid = compute_maker_bid(50.0, 0)
        assert bid == 50


class TestFillLogic:
    """Test fill detection logic."""
    
    def test_fill_when_ask_below_bid(self):
        # Ask=45, Bid=47 -> fill
        filled, price = check_fill(47, 45, "maker_at_bid")
        assert filled
        assert price == 47  # Fill at our bid
    
    def test_fill_when_ask_equals_bid(self):
        filled, price = check_fill(47, 47, "maker_at_bid")
        assert filled
        assert price == 47
    
    def test_no_fill_when_ask_above_bid(self):
        # Ask=50, Bid=47 -> no fill
        filled, price = check_fill(47, 50, "maker_at_bid")
        assert not filled
    
    def test_price_improve_model(self):
        # Ask=45, Bid=47 -> fill at ask (better price)
        filled, price = check_fill(47, 45, "price_improve_to_ask")
        assert filled
        assert price == 45  # Fill at ask


class TestFullSetAccumulator:
    """Test full strategy logic."""
    
    def make_tick(self, t: float, up_ask: int, up_bid: int, 
                  down_ask: int, down_bid: int) -> QuoteTick:
        return QuoteTick(
            elapsed_secs=t,
            up_ask=up_ask,
            up_bid=up_bid,
            down_ask=down_ask,
            down_bid=down_bid
        )
    
    def test_pair_completion_both_legs_fill(self):
        """Test that pair completes when both legs fill."""
        config = StrategyConfig(
            d_cents=3,
            chase_timeout_secs=30,
            max_pair_cost_cents=100,
            fill_model="maker_at_bid"
        )
        
        strategy = FullSetAccumulator(config)
        
        # Create ticks where both sides should fill
        # Tick 0: UP mid=(50+48)/2=49, bid=46. DOWN mid=(50+48)/2=49, bid=46
        # Tick 1: UP ask=45 <= 46 -> fill at 46. DOWN mid stays same, starts chase
        # Tick 2: DOWN ask=44 <= remaining bid (with chase) -> fill
        ticks = [
            self.make_tick(0.0, 50, 48, 50, 48),   # Initial: mids=49, bids=46
            self.make_tick(1.0, 45, 43, 55, 53),   # UP ask=45 <= 46 -> fills
            self.make_tick(2.0, 45, 43, 44, 42),   # DOWN ask=44 <= chase bid
        ]
        
        window = WindowData(window_id="test", ticks=ticks)
        state = strategy.run_window(window)
        
        # Should have completed one pair
        assert len(state.completed_pairs) == 1
        pair = state.completed_pairs[0]
        # UP filled at 46, DOWN should fill during chase
        assert pair.edge_cents > 0  # Should be profitable
    
    def test_chase_timeout_causes_unwind(self):
        """Test that chase timeout leads to unwind."""
        config = StrategyConfig(
            d_cents=3,
            chase_timeout_secs=5.0,
            max_pair_cost_cents=100,
            fill_model="maker_at_bid",
            slip_unwind_cents=1
        )
        
        strategy = FullSetAccumulator(config)
        
        # UP fills, DOWN never fills, timeout triggers unwind
        # Tick 0: UP mid=(50+48)/2=49, bid=46. DOWN mid=(50+48)/2=49, bid=46
        # Tick 1: UP ask=45 <= 46 -> fills, DOWN ask=80 way too high
        # Tick 3-7: DOWN ask stays high, chase times out at 5s
        ticks = [
            self.make_tick(0.0, 50, 48, 50, 48),   # Initial
            self.make_tick(1.0, 45, 43, 80, 78),   # UP fills, DOWN too high
            self.make_tick(3.0, 45, 43, 80, 78),   # Still waiting
            self.make_tick(7.0, 45, 43, 80, 78),   # Chase timeout at 5s, unwind
        ]
        
        window = WindowData(window_id="test", ticks=ticks)
        state = strategy.run_window(window)
        
        # No completed pairs, but should have unwind
        assert len(state.completed_pairs) == 0
        assert len(state.unwind_events) >= 1
    
    def test_max_pair_cost_caps_chase(self):
        """Test that chase bid is capped by max_pair_cost."""
        config = StrategyConfig(
            d_cents=3,
            chase_step_cents=5,
            chase_step_secs=1.0,
            chase_timeout_secs=30,
            max_pair_cost_cents=98,
            fill_model="maker_at_bid"
        )
        
        strategy = FullSetAccumulator(config)
        state = StrategyState(window_id="test", config=config)
        
        # Simulate UP leg filled at 50c
        state.up_leg.state = LegState.FILLED
        state.up_leg.fill_price = 50
        state.up_leg.fill_time = 0.0
        
        # DOWN leg chasing
        state.down_leg.state = LegState.CHASING
        state.down_leg.quote_bid = 45
        state.down_leg.chase_start_time = 0.0
        state.down_leg.last_chase_step_time = 0.0
        
        # Max DOWN bid = 98 - 50 = 48
        # Starting at 45, step of 5 would go to 50, but capped at 48
        tick = self.make_tick(2.0, 50, 45, 55, 50)
        state.current_time = 2.0
        
        strategy._process_chase(state, tick)
        
        # Bid should be capped at 48 (98 - 50)
        assert state.down_leg.quote_bid == 48


if __name__ == "__main__":
    pytest.main([__file__, "-v"])

