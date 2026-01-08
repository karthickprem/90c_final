"""Tests for balance module - portfolio vs cash handling"""

import pytest
from mm_bot.balance import AccountSnapshot, BalanceManager


class TestAccountSnapshot:
    """Test AccountSnapshot calculations"""
    
    def test_equity_calculation(self):
        """Test equity = cash + positions"""
        snap = AccountSnapshot(
            cash_available_usdc=5.05,
            positions_mtm_usdc=0.09
        )
        
        # Equity should be cash + positions
        assert abs(snap.equity_estimate_usdc - 5.14) < 0.01
    
    def test_spendable_calculation(self):
        """Test spendable = cash - locked - buffer"""
        snap = AccountSnapshot(
            cash_available_usdc=5.05,
            locked_usdc_in_open_buys=1.00,
            positions_mtm_usdc=0.09,
            safety_buffer=0.50
        )
        
        # Spendable = 5.05 - 1.00 - 0.50 = 3.55
        assert abs(snap.spendable_usdc - 3.55) < 0.01
    
    def test_spendable_never_negative(self):
        """Test spendable is clamped at zero"""
        snap = AccountSnapshot(
            cash_available_usdc=1.00,
            locked_usdc_in_open_buys=2.00,
            safety_buffer=0.50
        )
        
        # Would be 1.00 - 2.00 - 0.50 = -1.50, but clamped to 0
        assert snap.spendable_usdc == 0.0
    
    def test_can_place_order_notional_check(self):
        """Test order placement check for minimum notional"""
        snap = AccountSnapshot(
            cash_available_usdc=5.00,
            safety_buffer=0.50
        )
        
        # Notional below minimum
        can_place, reason = snap.can_place_order(0.50, min_notional=1.0)
        assert not can_place
        assert "min" in reason.lower()
    
    def test_can_place_order_spendable_check(self):
        """Test order placement check for spendable"""
        snap = AccountSnapshot(
            cash_available_usdc=2.00,
            locked_usdc_in_open_buys=1.00,
            safety_buffer=0.50
        )
        # Spendable = 2.00 - 1.00 - 0.50 = 0.50
        
        # Order that exceeds spendable
        can_place, reason = snap.can_place_order(1.50, min_notional=1.0)
        assert not can_place
        assert "spendable" in reason.lower()
    
    def test_can_place_order_success(self):
        """Test successful order placement check"""
        snap = AccountSnapshot(
            cash_available_usdc=5.00,
            locked_usdc_in_open_buys=0.00,
            safety_buffer=0.50
        )
        # Spendable = 5.00 - 0 - 0.50 = 4.50
        
        can_place, reason = snap.can_place_order(1.50, min_notional=1.0)
        assert can_place
        assert reason == ""
    
    def test_to_dict(self):
        """Test conversion to dict for logging"""
        snap = AccountSnapshot(
            cash_available_usdc=5.05,
            locked_usdc_in_open_buys=1.00,
            positions_mtm_usdc=0.09,
            safety_buffer=0.50
        )
        
        d = snap.to_dict()
        assert "cash_available" in d
        assert "locked_in_buys" in d
        assert "positions_mtm" in d
        assert "equity_estimate" in d
        assert "spendable" in d
    
    def test_to_log_string(self):
        """Test log string format"""
        snap = AccountSnapshot(
            cash_available_usdc=5.05,
            locked_usdc_in_open_buys=1.00,
            positions_mtm_usdc=0.09,
            safety_buffer=0.50
        )
        
        s = snap.to_log_string()
        assert "Cash" in s
        assert "Locked" in s
        assert "Positions" in s
        assert "Equity" in s
        assert "Spendable" in s


class TestMinNotionalHandling:
    """Test minimum notional calculations"""
    
    def test_low_price_requires_large_size(self):
        """At 1c, need 100+ shares for $1 notional"""
        snap = AccountSnapshot(cash_available_usdc=10.00)
        
        # At 0.01 price, 5 shares = $0.05 < $1 min
        can_place, reason = snap.can_place_order(0.05, min_notional=1.0)
        assert not can_place
        assert "min" in reason.lower()
        
        # At 0.01 price, 100 shares = $1.00 >= $1 min
        can_place, reason = snap.can_place_order(1.00, min_notional=1.0)
        assert can_place
    
    def test_high_price_allows_small_size(self):
        """At 50c, need only 2 shares for $1 notional"""
        snap = AccountSnapshot(cash_available_usdc=10.00)
        
        # At 0.50 price, 2 shares = $1.00 >= $1 min
        can_place, reason = snap.can_place_order(1.00, min_notional=1.0)
        assert can_place


class TestPortfolioVsCash:
    """Test that bot correctly uses cash, not portfolio"""
    
    def test_portfolio_higher_than_cash(self):
        """Common case: portfolio > cash due to positions"""
        snap = AccountSnapshot(
            cash_available_usdc=5.05,
            positions_mtm_usdc=0.09
        )
        
        # Portfolio is higher
        assert snap.equity_estimate_usdc > snap.cash_available_usdc
        
        # But spendable uses cash, not portfolio
        assert snap.spendable_usdc <= snap.cash_available_usdc
    
    def test_residual_positions_dont_increase_spendable(self):
        """Residual positions should NOT increase buying power"""
        snap_no_positions = AccountSnapshot(
            cash_available_usdc=5.00,
            positions_mtm_usdc=0.00,
            safety_buffer=0.50
        )
        
        snap_with_positions = AccountSnapshot(
            cash_available_usdc=5.00,
            positions_mtm_usdc=2.00,  # $2 in positions
            safety_buffer=0.50
        )
        
        # Spendable should be the SAME regardless of positions
        assert snap_no_positions.spendable_usdc == snap_with_positions.spendable_usdc
    
    def test_locked_reduces_spendable(self):
        """Locked USDC in buy orders reduces spendable"""
        snap_unlocked = AccountSnapshot(
            cash_available_usdc=5.00,
            locked_usdc_in_open_buys=0.00,
            safety_buffer=0.50
        )
        
        snap_locked = AccountSnapshot(
            cash_available_usdc=5.00,
            locked_usdc_in_open_buys=2.00,
            safety_buffer=0.50
        )
        
        # Locked reduces spendable
        assert snap_locked.spendable_usdc < snap_unlocked.spendable_usdc
        assert snap_locked.spendable_usdc == snap_unlocked.spendable_usdc - 2.00

