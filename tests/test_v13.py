"""
V13 Unit Tests
==============

Tests for:
a) Trade parsing (transactionHash presence, timestamp int/str)
b) Dedupe key correctness
c) Volatility calculation
d) State machine transitions (no close without SELL fill)
"""

import pytest
import time
import sys
import os

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from mm_bot.volatility import VolatilityTracker, VolatilitySnapshot, compute_vol_distribution
from mm_bot.fill_tracker_v13 import FillTrackerV13, FillTrackerError, FillSide


class TestVolatility:
    """Tests for volatility calculation."""
    
    def test_empty_tracker(self):
        """Empty tracker should return zero volatility."""
        tracker = VolatilityTracker(window_secs=10.0)
        snapshot = tracker.get_current()
        assert snapshot is None
    
    def test_single_sample(self):
        """Single sample should have zero volatility."""
        tracker = VolatilityTracker(window_secs=10.0)
        snapshot = tracker.update(0.50, timestamp=1000.0)
        
        assert snapshot.mid_now == 0.50
        assert snapshot.vol_10s_cents == 0.0
        assert snapshot.sample_count == 1
    
    def test_volatility_calculation(self):
        """Volatility should be (max - min) * 100."""
        tracker = VolatilityTracker(window_secs=10.0)
        
        # Add samples within 10s window
        tracker.update(0.50, timestamp=1000.0)
        tracker.update(0.52, timestamp=1002.0)
        tracker.update(0.48, timestamp=1004.0)
        snapshot = tracker.update(0.51, timestamp=1006.0)
        
        # Vol = (0.52 - 0.48) * 100 = 4c
        assert snapshot.vol_10s_cents == pytest.approx(4.0, abs=0.01)
        assert snapshot.mid_min == 0.48
        assert snapshot.mid_max == 0.52
    
    def test_window_pruning(self):
        """Old samples should be pruned after window expires."""
        tracker = VolatilityTracker(window_secs=10.0)
        
        # Add old sample
        tracker.update(0.30, timestamp=1000.0)  # Will be pruned
        
        # Add samples within window
        tracker.update(0.50, timestamp=1015.0)
        snapshot = tracker.update(0.51, timestamp=1016.0)
        
        # 0.30 should be pruned (older than 10s from 1016)
        assert snapshot.mid_min == 0.50
        assert snapshot.mid_max == 0.51
        assert snapshot.vol_10s_cents == pytest.approx(1.0, abs=0.01)
    
    def test_move_calculation(self):
        """Move should be abs(now - oldest) * 100."""
        tracker = VolatilityTracker(window_secs=10.0)
        
        tracker.update(0.50, timestamp=1000.0)
        tracker.update(0.55, timestamp=1005.0)
        snapshot = tracker.update(0.52, timestamp=1008.0)
        
        # Move = abs(0.52 - 0.50) * 100 = 2c
        assert snapshot.move_10s_cents == pytest.approx(2.0, abs=0.01)
    
    def test_distribution_computation(self):
        """Test distribution computation from tick data."""
        # Create synthetic ticks
        ticks = []
        for i in range(100):
            ts = 1000.0 + i * 0.5  # 0.5s intervals
            mid = 0.50 + 0.01 * (i % 5)  # Oscillate 0-4c
            ticks.append((ts, mid))
        
        dist = compute_vol_distribution(ticks, window_secs=10.0)
        
        assert 'P50' in dist
        assert 'P90' in dist
        assert 'P95' in dist
        assert dist['P95'] >= dist['P50']


class TestFillTracker:
    """Tests for fill tracker."""
    
    def test_dedupe_key(self):
        """Dedupe key should be deterministic."""
        tracker = FillTrackerV13()
        
        key1 = tracker._make_dedupe_key(
            "0xabc123", "token1", "BUY", 10.0, 0.50, 1000.0
        )
        key2 = tracker._make_dedupe_key(
            "0xabc123", "token1", "BUY", 10.0, 0.50, 1000.0
        )
        
        assert key1 == key2
        assert len(key1) == 32
    
    def test_dedupe_key_different(self):
        """Different trades should have different keys."""
        tracker = FillTrackerV13()
        
        key1 = tracker._make_dedupe_key(
            "0xabc123", "token1", "BUY", 10.0, 0.50, 1000.0
        )
        key2 = tracker._make_dedupe_key(
            "0xdef456", "token1", "BUY", 10.0, 0.50, 1000.0
        )
        
        assert key1 != key2
    
    def test_parse_trade_missing_txhash(self):
        """Missing transactionHash should trigger kill switch."""
        killed = []
        tracker = FillTrackerV13(
            on_kill_switch=lambda reason: killed.append(reason)
        )
        tracker.set_boundary(boundary_ts=0)
        
        trade = {
            "transactionHash": "",  # Missing!
            "asset": "token1",
            "side": "BUY",
            "size": 10,
            "price": 0.50,
            "timestamp": 1000
        }
        
        with pytest.raises(FillTrackerError):
            tracker._parse_trade(trade)
        
        assert len(killed) == 1
        assert "Missing transactionHash" in killed[0]
    
    def test_parse_trade_boundary(self):
        """Trades before boundary should be ignored."""
        tracker = FillTrackerV13()
        tracker.set_boundary(boundary_ts=1000.0)
        
        # Trade before boundary
        trade = {
            "transactionHash": "0xabc123456789",
            "asset": "token1",
            "side": "BUY",
            "size": 10,
            "price": 0.50,
            "timestamp": 999  # Before boundary
        }
        
        result = tracker._parse_trade(trade)
        assert result is None
    
    def test_parse_trade_timestamp_string(self):
        """String timestamps should be parsed correctly."""
        tracker = FillTrackerV13()
        tracker.set_boundary(boundary_ts=0)
        
        trade = {
            "transactionHash": "0xabc123456789",
            "asset": "token1",
            "side": "BUY",
            "size": 10,
            "price": 0.50,
            "timestamp": "1000"  # String!
        }
        
        result = tracker._parse_trade(trade)
        assert result is not None
        assert result.timestamp == 1000.0
    
    def test_parse_trade_timestamp_int(self):
        """Integer timestamps should be parsed correctly."""
        tracker = FillTrackerV13()
        tracker.set_boundary(boundary_ts=0)
        
        trade = {
            "transactionHash": "0xabc123456789",
            "asset": "token1",
            "side": "BUY",
            "size": 10,
            "price": 0.50,
            "timestamp": 1000  # Integer
        }
        
        result = tracker._parse_trade(trade)
        assert result is not None
        assert result.timestamp == 1000.0
    
    def test_position_opens_on_buy(self):
        """Position should only open on BUY fill."""
        tracker = FillTrackerV13()
        tracker.set_boundary(boundary_ts=0)
        
        # No position initially
        assert not tracker.has_open_position("token1")
        
        # Create a fill manually
        from mm_bot.fill_tracker_v13 import ConfirmedFill
        fill = ConfirmedFill(
            trade_id="test1",
            transaction_hash="0xabc",
            token_id="token1",
            side=FillSide.BUY,
            price=0.50,
            size=10.0,
            timestamp=1000.0
        )
        
        tracker._process_fill(fill)
        
        # Now should have position
        assert tracker.has_open_position("token1")
        assert tracker.get_confirmed_shares("token1") == 10.0
    
    def test_position_closes_on_sell(self):
        """Position should only close on SELL fill."""
        tracker = FillTrackerV13()
        tracker.set_boundary(boundary_ts=0)
        
        from mm_bot.fill_tracker_v13 import ConfirmedFill
        
        # Open position
        buy_fill = ConfirmedFill(
            trade_id="test1",
            transaction_hash="0xabc",
            token_id="token1",
            side=FillSide.BUY,
            price=0.50,
            size=10.0,
            timestamp=1000.0
        )
        tracker._process_fill(buy_fill)
        assert tracker.get_confirmed_shares("token1") == 10.0
        
        # Close position with SELL
        sell_fill = ConfirmedFill(
            trade_id="test2",
            transaction_hash="0xdef",
            token_id="token1",
            side=FillSide.SELL,
            price=0.51,
            size=10.0,
            timestamp=1001.0
        )
        tracker._process_fill(sell_fill)
        
        # Position should be closed
        assert tracker.get_confirmed_shares("token1") == 0.0
        assert not tracker.has_open_position("token1")
    
    def test_round_trip_pnl(self):
        """Round-trip PnL should be calculated correctly."""
        tracker = FillTrackerV13()
        tracker.set_boundary(boundary_ts=0)
        
        from mm_bot.fill_tracker_v13 import ConfirmedFill
        
        # Buy at 0.50
        buy_fill = ConfirmedFill(
            trade_id="test1",
            transaction_hash="0xabc",
            token_id="token1",
            side=FillSide.BUY,
            price=0.50,
            size=10.0,
            timestamp=1000.0
        )
        tracker._process_fill(buy_fill)
        
        # Sell at 0.52 (profit)
        sell_fill = ConfirmedFill(
            trade_id="test2",
            transaction_hash="0xdef",
            token_id="token1",
            side=FillSide.SELL,
            price=0.52,
            size=10.0,
            timestamp=1001.0
        )
        tracker._process_fill(sell_fill)
        
        # PnL = (0.52 - 0.50) * 10 = 0.20
        assert len(tracker.round_trips) == 1
        assert tracker.round_trips[0]['pnl'] == pytest.approx(0.20, abs=0.01)
    
    def test_no_close_without_sell(self):
        """Position should NOT close without SELL fill (no synthetic close)."""
        tracker = FillTrackerV13()
        tracker.set_boundary(boundary_ts=0)
        
        from mm_bot.fill_tracker_v13 import ConfirmedFill
        
        # Open position
        buy_fill = ConfirmedFill(
            trade_id="test1",
            transaction_hash="0xabc",
            token_id="token1",
            side=FillSide.BUY,
            price=0.50,
            size=10.0,
            timestamp=1000.0
        )
        tracker._process_fill(buy_fill)
        
        # Position should remain open (no SELL)
        assert tracker.has_open_position("token1")
        assert tracker.get_confirmed_shares("token1") == 10.0
        
        # Even if we call get_summary, position should remain
        summary = tracker.get_summary()
        assert summary['open_positions'] == 1
        assert summary['total_shares'] == 10.0


class TestStateMachine:
    """Tests for state machine transitions."""
    
    def test_no_synthetic_transitions(self):
        """
        State transitions should ONLY happen from fills.
        Reconcile data should NOT cause position changes.
        """
        tracker = FillTrackerV13()
        tracker.set_boundary(boundary_ts=0)
        
        # Initially no positions
        assert tracker.get_total_confirmed_shares() == 0
        
        # Simulating what reconcile might see (but we don't act on it)
        # There is no method to "set position from reconcile" - that's the point
        # The only way to change position is through _process_fill
        
        # Position remains 0 because no fills processed
        assert tracker.get_total_confirmed_shares() == 0


if __name__ == "__main__":
    pytest.main([__file__, "-v"])

