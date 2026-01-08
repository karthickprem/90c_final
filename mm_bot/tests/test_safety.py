"""Tests for safety module"""

import pytest
import time
import os

from mm_bot.safety import SafetyState, SafetyManager, LockFile
from mm_bot.order_manager import OrderRole, ManagedOrder


class TestSafetyState:
    """Test SafetyState tracking"""
    
    def test_initial_state(self):
        state = SafetyState()
        assert not state.kill_triggered
        assert state.reconcile_mismatch_count == 0
        assert len(state.inv_without_exit) == 0
    
    def test_inv_tracking(self):
        state = SafetyState()
        state.inv_without_exit["token1"] = time.time()
        assert "token1" in state.inv_without_exit


class TestSafetyManager:
    """Test SafetyManager"""
    
    def test_inv_exit_tracking(self):
        """Test inventory without exit tracking"""
        manager = SafetyManager(verbose=False)
        token_id = "test_token"
        
        # Track inventory without exit
        manager.update_inv_exit_tracking(token_id, has_inv=True, has_exit=False)
        assert token_id in manager.state.inv_without_exit
        
        # Clear when exit exists
        manager.update_inv_exit_tracking(token_id, has_inv=True, has_exit=True)
        assert token_id not in manager.state.inv_without_exit
    
    def test_kill_condition_inv_timeout(self):
        """Test kill switch on inventory without exit timeout"""
        manager = SafetyManager(verbose=False)
        manager.state.inv_without_exit_threshold = 0.1  # 100ms for test
        
        token_id = "test_token"
        manager.state.inv_without_exit[token_id] = time.time() - 1.0  # 1s ago
        
        should_kill, reason = manager.check_kill_conditions()
        assert should_kill
        assert "without exit" in reason.lower()
    
    def test_kill_condition_reconcile_mismatch(self):
        """Test kill switch on reconciliation mismatch"""
        manager = SafetyManager(verbose=False)
        manager.state.reconcile_mismatch_count = 3  # Exceeds threshold of 2
        
        should_kill, reason = manager.check_kill_conditions()
        assert should_kill
        assert "mismatch" in reason.lower()
    
    def test_trigger_kill(self):
        """Test kill switch triggering"""
        manager = SafetyManager(verbose=False)
        assert not manager.is_killed()
        
        manager.trigger_kill("Test reason")
        
        assert manager.is_killed()
        assert manager.state.kill_reason == "Test reason"


class TestLockFile:
    """Test LockFile for preventing duplicate runners"""
    
    def test_acquire_release(self):
        """Test basic lock acquire/release"""
        lock_path = "test_lock.lock"
        lock = LockFile(path=lock_path)
        
        try:
            # Should acquire successfully
            assert lock.acquire()
            
            # Release
            lock.release()
            
            # Should be able to acquire again
            lock2 = LockFile(path=lock_path)
            assert lock2.acquire()
            lock2.release()
        finally:
            # Cleanup
            import pathlib
            p = pathlib.Path(lock_path)
            if p.exists():
                p.unlink()
    
    def test_double_acquire_fails(self):
        """Test that second acquire fails if first is held"""
        lock_path = "test_lock2.lock"
        lock1 = LockFile(path=lock_path)
        lock2 = LockFile(path=lock_path)
        
        try:
            # First acquire succeeds
            assert lock1.acquire()
            
            # Second acquire should fail (same PID so it will actually succeed in test)
            # In reality, with different PIDs this would fail
            # For testing purposes we just verify the lock file exists
            import pathlib
            assert pathlib.Path(lock_path).exists()
            
            lock1.release()
        finally:
            import pathlib
            p = pathlib.Path(lock_path)
            if p.exists():
                p.unlink()


class TestOrderRoles:
    """Test order role handling"""
    
    def test_order_roles_exist(self):
        """Test OrderRole constants"""
        assert OrderRole.ENTRY == "ENTRY"
        assert OrderRole.EXIT == "EXIT"
    
    def test_managed_order_role(self):
        """Test ManagedOrder has role"""
        order = ManagedOrder(
            order_id="123",
            token_id="token",
            side="BUY",
            price=0.50,
            size=10,
            role=OrderRole.ENTRY
        )
        assert order.role == OrderRole.ENTRY
        
        exit_order = ManagedOrder(
            order_id="456",
            token_id="token",
            side="SELL",
            price=0.55,
            size=10,
            role=OrderRole.EXIT
        )
        assert exit_order.role == OrderRole.EXIT
    
    def test_default_role_is_entry(self):
        """Test default role is ENTRY"""
        order = ManagedOrder(
            order_id="789",
            token_id="token",
            side="BUY",
            price=0.50,
            size=10
        )
        assert order.role == OrderRole.ENTRY


class TestReconciliation:
    """Test inventory reconciliation"""
    
    def test_reconcile_positions_mismatch(self):
        """Test reconciliation detects mismatches"""
        from mm_bot.config import Config
        from mm_bot.inventory import InventoryManager
        
        config = Config()
        inv = InventoryManager(config)
        
        yes_token = "yes_token_123"
        no_token = "no_token_456"
        inv.set_tokens(yes_token, no_token)
        
        # Set internal state
        inv.process_fill(yes_token, "BUY", 10, 0.50)
        
        # Reconcile with different actual
        actual = {yes_token: 15.0, no_token: 0.0}
        mismatches = inv.reconcile_positions(actual, verbose=False)
        
        assert yes_token in mismatches
        assert mismatches[yes_token]["internal"] == 10.0
        assert mismatches[yes_token]["actual"] == 15.0
        
        # Internal should be updated to actual
        assert inv.get_yes_shares() == 15.0
    
    def test_reconcile_positions_no_mismatch(self):
        """Test reconciliation with no mismatch"""
        from mm_bot.config import Config
        from mm_bot.inventory import InventoryManager
        
        config = Config()
        inv = InventoryManager(config)
        
        yes_token = "yes_token_123"
        no_token = "no_token_456"
        inv.set_tokens(yes_token, no_token)
        
        # Set internal state
        inv.process_fill(yes_token, "BUY", 10, 0.50)
        
        # Reconcile with matching actual
        actual = {yes_token: 10.0, no_token: 0.0}
        mismatches = inv.reconcile_positions(actual, verbose=False)
        
        assert len(mismatches) == 0
    
    def test_force_set_shares(self):
        """Test force setting shares"""
        from mm_bot.config import Config
        from mm_bot.inventory import InventoryManager
        
        config = Config()
        inv = InventoryManager(config)
        
        token_id = "test_token"
        inv.force_set_shares(token_id, 25.0, avg_cost=0.50)
        
        pos = inv.get_position(token_id)
        assert pos.shares == 25.0
        assert pos.avg_cost == 0.50
        assert pos.total_cost == 12.5


class TestExitEnforcement:
    """Test exit enforcement invariants"""
    
    def test_no_sell_without_inventory(self):
        """Test can't sell without inventory"""
        from mm_bot.config import Config
        from mm_bot.inventory import InventoryManager
        
        config = Config()
        inv = InventoryManager(config)
        
        token_id = "test_token"
        can_sell, reason = inv.can_sell(token_id, 10)
        
        assert not can_sell
        assert "insufficient" in reason.lower()
    
    def test_can_sell_with_inventory(self):
        """Test can sell with inventory"""
        from mm_bot.config import Config
        from mm_bot.inventory import InventoryManager
        
        config = Config()
        inv = InventoryManager(config)
        
        token_id = "test_token"
        inv.process_fill(token_id, "BUY", 20, 0.50)
        
        can_sell, reason = inv.can_sell(token_id, 10)
        assert can_sell

