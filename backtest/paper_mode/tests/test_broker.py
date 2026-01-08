"""
Unit tests for paper broker and safety guards.
"""

import pytest

from ..paper_broker import PaperBroker
from ..polymarket_client import PolymarketClient


class TestPaperBroker:
    """Test paper broker position management."""
    
    def test_open_position(self):
        """Test opening a position."""
        broker = PaperBroker(starting_bankroll=100.0)
        
        pos = broker.open_position(
            window_id="test-1",
            side="UP",
            fill_price=90,  # 90 cents
            f=0.02,  # 2% of bankroll
        )
        
        assert pos is not None
        assert pos.side == "UP"
        assert pos.entry_price == 90
        assert pos.cost == 2.0  # 2% of $100
        assert pos.shares == pytest.approx(2.0 / 0.90, rel=0.01)
        
        # Bankroll reduced
        assert broker.bankroll == pytest.approx(98.0, rel=0.01)
    
    def test_close_position_profit(self):
        """Test closing a position with profit."""
        broker = PaperBroker(starting_bankroll=100.0)
        
        # Open at 90c
        broker.open_position("test-1", "UP", 90, 0.02)
        
        # Close at 100c (full win)
        trade = broker.close_position("test-1", 100, "SETTLEMENT_WIN")
        
        assert trade is not None
        assert trade.pnl_invested == pytest.approx(0.111, rel=0.01)  # (100-90)/90
        assert trade.pnl_dollars > 0
        
        # Bankroll should be higher
        assert broker.bankroll > 100.0
    
    def test_close_position_loss(self):
        """Test closing a position with loss."""
        broker = PaperBroker(starting_bankroll=100.0)
        
        # Open at 93c
        broker.open_position("test-1", "UP", 93, 0.02)
        
        # Close at 85c (SL hit)
        trade = broker.close_position("test-1", 85, "SL")
        
        assert trade is not None
        assert trade.pnl_invested < 0
        assert trade.pnl_dollars < 0
        
        # Bankroll should be lower
        assert broker.bankroll < 100.0
    
    def test_gap_detection(self):
        """Test gap event detection."""
        broker = PaperBroker(starting_bankroll=100.0)
        
        # Open at 93c
        broker.open_position("test-1", "UP", 93, 0.02)
        
        # Close at 65c (severe gap, -30%)
        trade = broker.close_position("test-1", 65, "SL")
        
        assert trade.is_gap  # loss > 15%
        assert trade.is_severe  # loss > 25% (-30% here)
    
    def test_no_double_open(self):
        """Should not allow opening second position."""
        broker = PaperBroker(starting_bankroll=100.0)
        
        # First position
        pos1 = broker.open_position("test-1", "UP", 90, 0.02)
        assert pos1 is not None
        
        # Second position should fail
        pos2 = broker.open_position("test-2", "DOWN", 90, 0.02)
        assert pos2 is None
    
    def test_stats(self):
        """Test statistics calculation."""
        broker = PaperBroker(starting_bankroll=100.0)
        
        # Trade 1: Win
        broker.open_position("test-1", "UP", 90, 0.02)
        broker.close_position("test-1", 100, "TP")
        
        # Trade 2: Loss
        broker.open_position("test-2", "DOWN", 93, 0.02)
        broker.close_position("test-2", 0, "SETTLEMENT_LOSS")
        
        stats = broker.get_stats()
        
        assert stats['trades'] == 2
        assert stats['wins'] == 1
        assert stats['losses'] == 1
        assert stats['gap_count'] >= 1  # The loss at 0 is definitely a gap


class TestPaperModeSafety:
    """Test that trading methods are disabled."""
    
    def test_place_order_disabled(self):
        """place_order must raise RuntimeError."""
        client = PolymarketClient()
        with pytest.raises(RuntimeError, match="PAPER MODE"):
            client.place_order("test", 100, 0.9)
    
    def test_cancel_order_disabled(self):
        """cancel_order must raise RuntimeError."""
        client = PolymarketClient()
        with pytest.raises(RuntimeError, match="PAPER MODE"):
            client.cancel_order("order_id")
    
    def test_execute_trade_disabled(self):
        """execute_trade must raise RuntimeError."""
        client = PolymarketClient()
        with pytest.raises(RuntimeError, match="PAPER MODE"):
            client.execute_trade()
    
    def test_post_order_disabled(self):
        """post_order must raise RuntimeError."""
        client = PolymarketClient()
        with pytest.raises(RuntimeError, match="PAPER MODE"):
            client.post_order({"order": "data"})

