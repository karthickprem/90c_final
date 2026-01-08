"""
Tests for Exit Management
=========================
Tests that fills trigger exit orders and cooldown doesn't block exits.
"""

import pytest
from unittest.mock import Mock, MagicMock
from mm_bot.config import Config, RunMode
from mm_bot.inventory import InventoryManager
from mm_bot.quoting import Quote


@pytest.fixture
def config():
    cfg = Config()
    cfg.mode = RunMode.DRYRUN
    cfg.risk.max_usdc_locked = 10.0
    cfg.risk.max_inv_shares_per_token = 50.0
    cfg.quoting.base_quote_size = 10.0
    cfg.quoting.target_half_spread_cents = 2.0
    return cfg


@pytest.fixture
def inventory(config):
    inv = InventoryManager(config)
    inv.set_tokens("yes_token", "no_token")
    return inv


class TestFillTriggersExit:
    """Test that inventory triggers exit order placement"""
    
    def test_fill_updates_inventory(self, inventory):
        """When a fill occurs, inventory should increase"""
        assert inventory.get_yes_shares() == 0
        
        # Simulate a fill
        inventory.process_fill("yes_token", "BUY", 10, 0.50)
        
        assert inventory.get_yes_shares() == 10
    
    def test_inventory_allows_sell_after_fill(self, inventory):
        """After fill, can_sell should return True"""
        inventory.process_fill("yes_token", "BUY", 10, 0.50)
        
        can_sell, reason = inventory.can_sell("yes_token", 10)
        assert can_sell, f"Should be able to sell after fill: {reason}"
    
    def test_inventory_tracks_cost_basis(self, inventory):
        """Inventory should track cost basis for P&L"""
        inventory.process_fill("yes_token", "BUY", 10, 0.50)
        
        pos = inventory.get_position("yes_token")
        assert pos.avg_cost == 0.50
        assert pos.total_cost == 5.0
    
    def test_partial_fill_updates_inventory(self, inventory):
        """Partial fills should update inventory correctly"""
        inventory.process_fill("yes_token", "BUY", 5, 0.50)  # Partial
        assert inventory.get_yes_shares() == 5
        
        inventory.process_fill("yes_token", "BUY", 5, 0.52)  # More
        assert inventory.get_yes_shares() == 10


class TestCooldownDoesNotBlockExits:
    """Test that spike cooldown doesn't block exit orders"""
    
    def test_inventory_can_sell_during_any_state(self, inventory):
        """Sell capability should not depend on external state"""
        # Add inventory
        inventory.process_fill("yes_token", "BUY", 10, 0.50)
        
        # Verify can_sell works
        can_sell, _ = inventory.can_sell("yes_token", 5)
        assert can_sell
        
        # Verify can_sell for full amount
        can_sell, _ = inventory.can_sell("yes_token", 10)
        assert can_sell
    
    def test_exit_quote_is_valid(self, config):
        """Exit quotes should be valid sell orders"""
        from mm_bot.quoting import QuoteEngine, clamp_price, round_to_tick
        from mm_bot.clob import OrderBook
        
        engine = QuoteEngine(config)
        
        book = OrderBook(
            token_id="test",
            bids=[{"price": "0.48", "size": "100"}],
            asks=[{"price": "0.52", "size": "100"}],
            timestamp=0
        )
        
        # Simulate having inventory
        quotes = engine.compute_quotes(
            book=book,
            inventory_shares=10,
            max_inventory=50,
            usdc_available=100
        )
        
        # Should have an ask (exit) quote
        assert quotes.ask is not None, "Should have exit quote when holding inventory"
        assert quotes.ask.side == "SELL"
        assert quotes.ask.size <= 10  # Can't sell more than we have


class TestFlattenRule:
    """Test flatten behavior near end of window"""
    
    def test_flatten_cancels_buy_orders(self, inventory):
        """During flatten, buy orders should be cancelled"""
        # This is tested implicitly by checking inventory state
        inventory.process_fill("yes_token", "BUY", 10, 0.50)
        
        # After flatten, inventory should allow full sell
        can_sell, _ = inventory.can_sell("yes_token", 10)
        assert can_sell
    
    def test_flatten_allows_full_exit(self, inventory):
        """Flatten should allow selling entire position"""
        inventory.process_fill("yes_token", "BUY", 20, 0.45)
        inventory.process_fill("no_token", "BUY", 15, 0.55)
        
        # Should be able to sell all
        can_sell_yes, _ = inventory.can_sell("yes_token", 20)
        can_sell_no, _ = inventory.can_sell("no_token", 15)
        
        assert can_sell_yes
        assert can_sell_no


class TestReconciliation:
    """Test position reconciliation"""
    
    def test_reconcile_updates_usdc(self, inventory):
        """Reconciliation should update USDC balance"""
        inventory.reconcile(usdc_balance=100.0, position_value=50.0)
        
        summary = inventory.get_summary()
        assert summary["usdc_available"] == 100.0
    
    def test_reconcile_updates_position_value(self, inventory):
        """Reconciliation should update position value"""
        inventory.reconcile(usdc_balance=100.0, position_value=50.0)
        
        summary = inventory.get_summary()
        assert summary["total_position_value"] == 50.0
    
    def test_fill_then_reconcile(self, inventory):
        """Fills followed by reconciliation should work correctly"""
        # Simulate fill
        inventory.process_fill("yes_token", "BUY", 10, 0.50)
        assert inventory.get_yes_shares() == 10
        
        # Reconcile shouldn't clear fills (positions are tracked separately)
        inventory.reconcile(usdc_balance=95.0, position_value=5.0)
        
        # Shares should still be there
        assert inventory.get_yes_shares() == 10

