"""
Tests for Inventory Manager
===========================
Tests position tracking and risk checks.
"""

import pytest
from mm_bot.config import Config
from mm_bot.inventory import InventoryManager, Position


@pytest.fixture
def config():
    cfg = Config()
    cfg.risk.max_usdc_locked = 10.0
    cfg.risk.max_inv_shares_per_token = 50.0
    cfg.risk.kill_switch_inv_threshold = 100.0
    cfg.risk.kill_switch_loss_threshold = 5.0
    return cfg


@pytest.fixture
def inventory(config):
    inv = InventoryManager(config)
    inv.set_tokens("yes_token", "no_token")
    return inv


class TestPosition:
    def test_add_buy(self):
        pos = Position(token_id="test")
        pos.add_buy(10, 0.50)
        
        assert pos.shares == 10
        assert pos.avg_cost == 0.50
        assert pos.total_cost == 5.0
    
    def test_add_multiple_buys(self):
        pos = Position(token_id="test")
        pos.add_buy(10, 0.40)  # 10 @ 0.40 = $4
        pos.add_buy(10, 0.60)  # 10 @ 0.60 = $6
        
        assert pos.shares == 20
        assert pos.total_cost == 10.0
        assert pos.avg_cost == 0.50  # $10 / 20 shares
    
    def test_add_sell(self):
        pos = Position(token_id="test")
        pos.add_buy(20, 0.50)  # Cost $10
        pos.add_sell(10, 0.60)  # Sell half
        
        assert pos.shares == 10
        assert pos.total_cost == 5.0  # Half the cost
    
    def test_sell_all(self):
        pos = Position(token_id="test")
        pos.add_buy(10, 0.50)
        pos.add_sell(10, 0.60)
        
        assert pos.shares == 0
        assert pos.total_cost == 0
        assert pos.avg_cost == 0
    
    def test_unrealized_pnl(self):
        pos = Position(token_id="test")
        pos.add_buy(10, 0.40)  # Cost $4
        
        # If we win, value is $10 (10 shares * $1)
        # Unrealized P&L = $10 - $4 = $6
        assert pos.unrealized_pnl == 6.0


class TestInventoryManager:
    def test_can_buy_within_limits(self, inventory):
        can, reason = inventory.can_buy("yes_token", 10, 0.50)
        assert can, f"Should be able to buy: {reason}"
    
    def test_cannot_buy_exceeds_usdc(self, inventory):
        # Max USDC locked is $10, trying to lock $20
        can, reason = inventory.can_buy("yes_token", 40, 0.50)
        assert not can, "Should not be able to buy - exceeds USDC limit"
        assert "usdc" in reason.lower()
    
    def test_cannot_buy_exceeds_inventory(self, inventory):
        # Max shares is 50, trying to buy 60
        can, reason = inventory.can_buy("yes_token", 60, 0.10)
        assert not can, "Should not be able to buy - exceeds inventory limit"
        assert "max_inv" in reason.lower()
    
    def test_can_sell_with_inventory(self, inventory):
        # First buy some
        inventory.process_fill("yes_token", "BUY", 20, 0.50)
        
        can, reason = inventory.can_sell("yes_token", 10)
        assert can, f"Should be able to sell: {reason}"
    
    def test_cannot_sell_no_inventory(self, inventory):
        can, reason = inventory.can_sell("yes_token", 10)
        assert not can, "Should not be able to sell - no inventory"
        assert "insufficient" in reason.lower()
    
    def test_cannot_sell_more_than_held(self, inventory):
        inventory.process_fill("yes_token", "BUY", 10, 0.50)
        
        can, reason = inventory.can_sell("yes_token", 20)
        assert not can, "Should not be able to sell more than held"
    
    def test_kill_switch_inventory_threshold(self, inventory):
        # Add lots of inventory
        inventory.process_fill("yes_token", "BUY", 60, 0.50)
        inventory.process_fill("no_token", "BUY", 50, 0.50)
        
        trigger, reason = inventory.check_kill_switch()
        assert trigger, "Kill switch should trigger on high inventory"
    
    def test_kill_switch_not_triggered_normal(self, inventory):
        inventory.process_fill("yes_token", "BUY", 10, 0.50)
        
        trigger, reason = inventory.check_kill_switch()
        assert not trigger, f"Kill switch should not trigger: {reason}"
    
    def test_process_fill_updates_shares(self, inventory):
        inventory.process_fill("yes_token", "BUY", 10, 0.50)
        
        assert inventory.get_yes_shares() == 10
        assert inventory.get_no_shares() == 0
    
    def test_summary(self, inventory):
        inventory.process_fill("yes_token", "BUY", 10, 0.50)
        inventory.process_fill("no_token", "BUY", 5, 0.40)
        
        summary = inventory.get_summary()
        assert summary["yes_shares"] == 10
        assert summary["no_shares"] == 5

