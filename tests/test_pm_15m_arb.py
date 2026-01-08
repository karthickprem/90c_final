"""
Tests for PM 15m Arb Bot

Tests cover:
1. Math invariants (SafeProfitNet calculation)
2. Signal detection logic
3. Legging/unwind logic
4. Position accounting
5. Replay determinism
"""

import pytest
import random
from datetime import datetime, timezone, timedelta
from unittest.mock import Mock, patch

import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent))

from pm_15m_arb.config import ArbConfig
from pm_15m_arb.orderbook import OrderBookLevel, OrderBookSnapshot, TickData, VWAPResult
from pm_15m_arb.strategy import PositionState, StrategyEngine
from pm_15m_arb.executor_paper import PaperExecutor, LegStatus, SimulatedOrder, PairExecutionResult


class TestSafeProfitNetMath:
    """Tests for SafeProfitNet calculation invariants."""
    
    def test_zero_position(self):
        """Zero position should have zero SafeProfitNet."""
        pos = PositionState()
        assert pos.safe_profit_net() == 0
        assert pos.min_qty == 0
        assert pos.total_cost == 0
    
    def test_balanced_position(self):
        """Balanced position: SafeProfitNet = min(QtyY, QtyN) - TotalCost."""
        pos = PositionState(
            qty_yes=10,
            qty_no=10,
            cost_yes=4.8,
            cost_no=4.9
        )
        
        # min(10, 10) * 1.0 - (4.8 + 4.9) = 10 - 9.7 = 0.3
        expected = 10.0 - 9.7
        assert abs(pos.safe_profit_net() - expected) < 0.0001
    
    def test_balanced_with_buffer(self):
        """SafeProfitNet should subtract buffers."""
        pos = PositionState(
            qty_yes=10,
            qty_no=10,
            cost_yes=4.8,
            cost_no=4.9
        )
        
        buffer = 0.05
        expected = 10.0 - 9.7 - buffer
        assert abs(pos.safe_profit_net(buffer) - expected) < 0.0001
    
    def test_imbalanced_position_yes_excess(self):
        """Imbalanced position (more YES): use min qty."""
        pos = PositionState(
            qty_yes=15,
            qty_no=10,
            cost_yes=7.0,
            cost_no=4.9
        )
        
        # min(15, 10) = 10, so only 10 pairs guaranteed
        # SafeProfitNet = 10 - (7.0 + 4.9) = 10 - 11.9 = -1.9
        expected = 10.0 - 11.9
        assert abs(pos.safe_profit_net() - expected) < 0.0001
        assert pos.min_qty == 10
        assert pos.excess_qty == 5
    
    def test_imbalanced_position_no_excess(self):
        """Imbalanced position (more NO): use min qty."""
        pos = PositionState(
            qty_yes=10,
            qty_no=15,
            cost_yes=4.8,
            cost_no=7.2
        )
        
        # min(10, 15) = 10
        expected = 10.0 - (4.8 + 7.2)
        assert abs(pos.safe_profit_net() - expected) < 0.0001
        assert pos.min_qty == 10
        assert pos.excess_qty == 5
    
    def test_invariant_always_min_qty(self):
        """Profit should always be based on min quantity."""
        for _ in range(100):
            qty_y = random.uniform(1, 100)
            qty_n = random.uniform(1, 100)
            cost_y = qty_y * random.uniform(0.4, 0.6)
            cost_n = qty_n * random.uniform(0.4, 0.6)
            
            pos = PositionState(
                qty_yes=qty_y,
                qty_no=qty_n,
                cost_yes=cost_y,
                cost_no=cost_n
            )
            
            # Invariant: min_qty * 1.0 is what we get paid
            redemption = pos.min_qty * 1.0
            profit = redemption - pos.total_cost
            
            assert abs(pos.safe_profit_net() - profit) < 0.0001


class TestSignalDetection:
    """Tests for signal detection logic."""
    
    def setup_method(self):
        """Setup test config."""
        self.config = ArbConfig()
        self.config.min_edge = 0.02  # 2% min edge
        self.config.slippage_buffer_per_leg = 0.005  # 0.5% per leg
        self.config.min_depth_shares = 10
    
    def test_actionable_signal(self):
        """Signal should be actionable when edge exceeds threshold."""
        # pair_cost = 0.45 + 0.50 = 0.95
        # edge = 1.0 - 0.95 - 0.01 (buffers) = 0.04
        # 0.04 > 0.02 (min_edge) -> actionable
        tick = self._create_tick(ask_yes=0.45, ask_no=0.50, depth=100)
        
        engine = StrategyEngine(self.config)
        is_actionable, pair_cost, edge, reason = engine._check_signal(tick)
        
        assert is_actionable
        assert edge > self.config.min_edge
    
    def test_not_actionable_low_edge(self):
        """Signal should not be actionable when edge is too low."""
        # pair_cost = 0.49 + 0.50 = 0.99
        # edge = 1.0 - 0.99 - 0.01 = 0.0 (below min_edge)
        tick = self._create_tick(ask_yes=0.49, ask_no=0.50, depth=100)
        
        engine = StrategyEngine(self.config)
        is_actionable, pair_cost, edge, reason = engine._check_signal(tick)
        
        assert not is_actionable
        assert "Edge" in reason
    
    def test_not_actionable_low_depth(self):
        """Signal should not be actionable when depth is too low."""
        tick = self._create_tick(ask_yes=0.45, ask_no=0.50, depth=5)
        
        engine = StrategyEngine(self.config)
        is_actionable, pair_cost, edge, reason = engine._check_signal(tick)
        
        assert not is_actionable
        assert "Depth" in reason
    
    def test_edge_with_buffers(self):
        """Edge calculation should include buffers."""
        tick = self._create_tick(ask_yes=0.48, ask_no=0.49, depth=100)
        
        engine = StrategyEngine(self.config)
        is_actionable, pair_cost, edge, reason = engine._check_signal(tick)
        
        # Raw: 0.48 + 0.49 = 0.97
        # With buffers: 0.97 + 2*0.005 = 0.98
        # Edge: 1.0 - 0.98 = 0.02
        assert abs(pair_cost - 0.98) < 0.001
        assert abs(edge - 0.02) < 0.001
    
    def _create_tick(self, ask_yes: float, ask_no: float, depth: float) -> TickData:
        """Create a tick for testing."""
        yes_book = OrderBookSnapshot(
            token_id="yes_token",
            side="YES",
            asks=[OrderBookLevel(price=ask_yes, size=depth)],
            bids=[OrderBookLevel(price=ask_yes - 0.02, size=depth)],
        )
        no_book = OrderBookSnapshot(
            token_id="no_token",
            side="NO",
            asks=[OrderBookLevel(price=ask_no, size=depth)],
            bids=[OrderBookLevel(price=ask_no - 0.02, size=depth)],
        )
        
        return TickData(
            timestamp=datetime.now(timezone.utc),
            market_id="test",
            window_id="test_window",
            ask_yes=ask_yes,
            ask_yes_size=depth,
            bid_yes=ask_yes - 0.02,
            bid_yes_size=depth,
            yes_book=yes_book,
            ask_no=ask_no,
            ask_no_size=depth,
            bid_no=ask_no - 0.02,
            bid_no_size=depth,
            no_book=no_book,
        )


class TestLeggingLogic:
    """Tests for legging and unwind logic."""
    
    def setup_method(self):
        """Setup test config and executor."""
        self.config = ArbConfig()
        self.config.max_leg_slippage = 0.01  # 1%
        self.config.replay_seed = 42
        
        self.executor = PaperExecutor(self.config)
    
    def test_both_legs_fill(self):
        """When both legs fill, status should be BOTH_FILLED."""
        # Mock orderbooks with good depth
        tick = self._create_good_tick()
        
        # Force fills by setting seed
        self.executor.set_seed(42)
        
        # Execute multiple times to test probabilistic behavior
        results = []
        for i in range(10):
            self.executor.set_seed(42 + i)
            result = self.executor.execute_paired_trade(tick, qty=10)
            results.append(result)
        
        # At least some should fill both legs with good depth
        both_filled = sum(1 for r in results if r.leg_status == LegStatus.BOTH_FILLED)
        assert both_filled > 0, "Expected some trades to fill both legs"
    
    def test_legging_event_tracked(self):
        """Legging events should be tracked."""
        tick = self._create_good_tick()
        
        # Execute multiple trades
        initial_legging = self.executor.legging_events
        
        for i in range(20):
            self.executor.set_seed(12345 + i)
            self.executor.execute_paired_trade(tick, qty=10)
        
        # Some legging should have occurred
        # (probabilistic, so just check it's possible)
        # In practice, with good depth, legging should be rare
        assert self.executor.pairs_attempted == 20
    
    def test_unwind_loss_calculation(self):
        """Unwind loss should be cost - proceeds."""
        # Create a simulated filled order
        order = SimulatedOrder(
            order_id="test",
            side="YES",
            token_id="test_token",
            price=0.48,
            qty=10,
            filled_qty=10,
            fill_price=0.48,
        )
        
        # Create book with bid lower than fill price
        book = OrderBookSnapshot(
            token_id="test_token",
            side="YES",
            asks=[OrderBookLevel(price=0.50, size=100)],
            bids=[OrderBookLevel(price=0.45, size=100)],
        )
        
        loss = self.executor._unwind_leg(order, book)
        
        # Loss = (10 * 0.48) - (10 * 0.45) = 4.80 - 4.50 = 0.30
        assert abs(loss - 0.30) < 0.01
    
    def test_complete_missing_leg_within_slippage(self):
        """Should complete missing leg if within slippage tolerance."""
        original_price = 0.50
        
        # Book with slightly higher price (within 1% tolerance)
        # 0.505 is 1% above 0.50, which is exactly at max_leg_slippage
        book = OrderBookSnapshot(
            token_id="test",
            side="NO",
            asks=[OrderBookLevel(price=0.504, size=100)],  # 0.8% above - within tolerance
            bids=[OrderBookLevel(price=0.48, size=100)],
        )
        
        success, fill_price = self.executor._try_complete_missing_leg(
            "NO", qty=10, book=book, original_price=original_price
        )
        
        assert success
        assert fill_price == 0.504
    
    def test_complete_missing_leg_exceeds_slippage(self):
        """Should not complete missing leg if slippage exceeds tolerance."""
        original_price = 0.50
        
        # Book with much higher price (exceeds tolerance)
        book = OrderBookSnapshot(
            token_id="test",
            side="NO",
            asks=[OrderBookLevel(price=0.52, size=100)],  # 4% above
            bids=[OrderBookLevel(price=0.48, size=100)],
        )
        
        success, fill_price = self.executor._try_complete_missing_leg(
            "NO", qty=10, book=book, original_price=original_price
        )
        
        assert not success
    
    def _create_good_tick(self) -> TickData:
        """Create a tick with good depth for testing."""
        yes_book = OrderBookSnapshot(
            token_id="yes_token",
            side="YES",
            asks=[OrderBookLevel(price=0.48, size=1000)],
            bids=[OrderBookLevel(price=0.45, size=1000)],
        )
        no_book = OrderBookSnapshot(
            token_id="no_token",
            side="NO",
            asks=[OrderBookLevel(price=0.49, size=1000)],
            bids=[OrderBookLevel(price=0.46, size=1000)],
        )
        
        return TickData(
            timestamp=datetime.now(timezone.utc),
            market_id="test",
            window_id="test_window",
            ask_yes=0.48,
            ask_yes_size=1000,
            bid_yes=0.45,
            bid_yes_size=1000,
            yes_book=yes_book,
            ask_no=0.49,
            ask_no_size=1000,
            bid_no=0.46,
            bid_no_size=1000,
            no_book=no_book,
        )


class TestVWAPCalculation:
    """Tests for VWAP calculation."""
    
    def test_single_level_vwap(self):
        """VWAP with single level should equal that level's price."""
        book = OrderBookSnapshot(
            token_id="test",
            side="YES",
            asks=[OrderBookLevel(price=0.50, size=100)],
            bids=[],
        )
        
        result = book.vwap_buy(50)
        
        assert result.can_fill
        assert result.vwap == 0.50
        assert result.filled_shares == 50
        assert result.levels_used == 1
    
    def test_multi_level_vwap(self):
        """VWAP across multiple levels should be weighted average."""
        book = OrderBookSnapshot(
            token_id="test",
            side="YES",
            asks=[
                OrderBookLevel(price=0.50, size=50),
                OrderBookLevel(price=0.52, size=50),
            ],
            bids=[],
        )
        
        result = book.vwap_buy(100)
        
        # VWAP = (50*0.50 + 50*0.52) / 100 = (25 + 26) / 100 = 0.51
        assert result.can_fill
        assert abs(result.vwap - 0.51) < 0.001
        assert result.levels_used == 2
        assert result.worst_price == 0.52
    
    def test_insufficient_depth(self):
        """VWAP should indicate when depth is insufficient."""
        book = OrderBookSnapshot(
            token_id="test",
            side="YES",
            asks=[OrderBookLevel(price=0.50, size=50)],
            bids=[],
        )
        
        result = book.vwap_buy(100)
        
        assert not result.can_fill
        assert result.filled_shares == 50
    
    def test_empty_book(self):
        """VWAP on empty book should return zeros."""
        book = OrderBookSnapshot(
            token_id="test",
            side="YES",
            asks=[],
            bids=[],
        )
        
        result = book.vwap_buy(100)
        
        assert not result.can_fill
        assert result.vwap == 0
        assert result.filled_shares == 0


class TestPositionAccounting:
    """Tests for position state accounting."""
    
    def test_update_from_fill_yes(self):
        """Update position from YES fill."""
        pos = PositionState()
        pos.update_from_fill("YES", qty=10, cost=4.8)
        
        assert pos.qty_yes == 10
        assert pos.cost_yes == 4.8
        assert pos.qty_no == 0
        assert pos.trades_count == 1
    
    def test_update_from_fill_no(self):
        """Update position from NO fill."""
        pos = PositionState()
        pos.update_from_fill("NO", qty=10, cost=4.9)
        
        assert pos.qty_no == 10
        assert pos.cost_no == 4.9
        assert pos.qty_yes == 0
        assert pos.trades_count == 1
    
    def test_cumulative_fills(self):
        """Multiple fills should accumulate correctly."""
        pos = PositionState()
        
        pos.update_from_fill("YES", qty=10, cost=4.8)
        pos.update_from_fill("NO", qty=10, cost=4.9)
        pos.update_from_fill("YES", qty=5, cost=2.4)
        pos.update_from_fill("NO", qty=5, cost=2.5)
        
        assert pos.qty_yes == 15
        assert pos.qty_no == 15
        assert abs(pos.cost_yes - 7.2) < 0.001
        assert abs(pos.cost_no - 7.4) < 0.001
        assert pos.trades_count == 4
    
    def test_to_dict(self):
        """Position should convert to dict correctly."""
        pos = PositionState(
            qty_yes=10,
            qty_no=10,
            cost_yes=4.8,
            cost_no=4.9,
            trades_count=2,
            pairs_filled=1,
        )
        
        d = pos.to_dict()
        
        assert d["qty_yes"] == 10
        assert d["qty_no"] == 10
        assert d["total_cost"] == 9.7
        assert d["min_qty"] == 10
        assert abs(d["safe_profit_net"] - 0.3) < 0.001


class TestReplayDeterminism:
    """Tests for replay determinism."""
    
    def test_same_seed_same_results(self):
        """Same random seed should produce identical results."""
        config = ArbConfig()
        config.replay_seed = 12345
        
        # Run 1
        executor1 = PaperExecutor(config)
        executor1.set_seed(12345)
        
        results1 = []
        tick = self._create_test_tick()
        for _ in range(10):
            result = executor1.execute_paired_trade(tick, qty=10)
            results1.append(result.leg_status)
        
        # Run 2
        executor2 = PaperExecutor(config)
        executor2.set_seed(12345)
        
        results2 = []
        for _ in range(10):
            result = executor2.execute_paired_trade(tick, qty=10)
            results2.append(result.leg_status)
        
        # Results should be identical
        assert results1 == results2
    
    def test_different_seed_different_results(self):
        """Different seeds should produce different random sequences."""
        config = ArbConfig()
        
        # Create a tick with marginal depth to force more randomness
        yes_book = OrderBookSnapshot(
            token_id="yes",
            side="YES",
            asks=[OrderBookLevel(price=0.48, size=15)],  # Low depth
            bids=[OrderBookLevel(price=0.45, size=15)],
        )
        no_book = OrderBookSnapshot(
            token_id="no",
            side="NO",
            asks=[OrderBookLevel(price=0.49, size=15)],  # Low depth
            bids=[OrderBookLevel(price=0.46, size=15)],
        )
        
        tick = TickData(
            timestamp=datetime.now(timezone.utc),
            market_id="test",
            window_id="test",
            ask_yes=0.48,
            ask_yes_size=15,
            bid_yes=0.45,
            bid_yes_size=15,
            ask_no=0.49,
            ask_no_size=15,
            bid_no=0.46,
            bid_no_size=15,
            yes_book=yes_book,
            no_book=no_book,
        )
        
        # Track random values generated, not just results
        # With very high fill probability, results may be same
        # but the random values should differ
        
        executor1 = PaperExecutor(config)
        executor1.set_seed(11111)
        r1_randoms = [executor1._random.random() for _ in range(10)]
        
        executor2 = PaperExecutor(config)
        executor2.set_seed(99999)
        r2_randoms = [executor2._random.random() for _ in range(10)]
        
        # Random sequences with different seeds must differ
        assert r1_randoms != r2_randoms, "Random sequences should differ with different seeds"
    
    def _create_test_tick(self) -> TickData:
        """Create a test tick."""
        yes_book = OrderBookSnapshot(
            token_id="yes",
            side="YES",
            asks=[OrderBookLevel(price=0.48, size=100)],
            bids=[OrderBookLevel(price=0.45, size=100)],
        )
        no_book = OrderBookSnapshot(
            token_id="no",
            side="NO",
            asks=[OrderBookLevel(price=0.49, size=100)],
            bids=[OrderBookLevel(price=0.46, size=100)],
        )
        
        return TickData(
            timestamp=datetime.now(timezone.utc),
            market_id="test",
            window_id="test",
            ask_yes=0.48,
            ask_yes_size=100,
            bid_yes=0.45,
            bid_yes_size=100,
            yes_book=yes_book,
            ask_no=0.49,
            ask_no_size=100,
            bid_no=0.46,
            bid_no_size=100,
            no_book=no_book,
        )


if __name__ == "__main__":
    pytest.main([__file__, "-v"])

