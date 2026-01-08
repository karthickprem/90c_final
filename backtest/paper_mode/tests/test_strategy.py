"""
Unit tests for paper mode strategy state machine.

Tests verify:
1. Trigger detection works correctly
2. SPIKE validation filters correctly
3. JUMP gate rejects volatile windows
4. TP/SL exits work as expected
5. Settlement logic is correct
"""

import pytest
import time

from ..config import StrategyConfig
from ..strategy import StrategyStateMachine, Tick, State


def make_tick(ts: float, up: int, down: int) -> Tick:
    """Helper to create a tick."""
    return Tick(ts=ts, up_cents=up, down_cents=down)


class TestTriggerDetection:
    """Test first-touch trigger logic."""
    
    def test_up_trigger(self):
        """UP >= 90 should trigger."""
        sm = StrategyStateMachine(StrategyConfig())
        sm.start_window("test", 0, 900)
        
        # No trigger at 89
        result = sm.process_tick(make_tick(1, 89, 50), 100)
        assert sm.state == State.IDLE
        
        # Trigger at 90
        result = sm.process_tick(make_tick(2, 90, 50), 100)
        assert sm.state == State.OBSERVE_10S
        assert sm.context.trigger_side == 'UP'
        assert sm.context.trigger_price == 90
    
    def test_down_trigger(self):
        """DOWN >= 90 should trigger."""
        sm = StrategyStateMachine(StrategyConfig())
        sm.start_window("test", 0, 900)
        
        result = sm.process_tick(make_tick(1, 50, 91), 100)
        assert sm.state == State.OBSERVE_10S
        assert sm.context.trigger_side == 'DOWN'
        assert sm.context.trigger_price == 91
    
    def test_tie_skip(self):
        """Both sides >= 90 in same tick should skip."""
        sm = StrategyStateMachine(StrategyConfig())
        sm.start_window("test", 0, 900)
        
        result = sm.process_tick(make_tick(1, 92, 90), 100)
        assert sm.state == State.DONE
        assert result.get('reason') == 'TIE_SKIP'


class TestSpikeValidation:
    """Test SPIKE filter (min >= 88, max >= 93)."""
    
    def test_spike_pass(self):
        """Valid spike should pass to entry."""
        cfg = StrategyConfig()
        sm = StrategyStateMachine(cfg)
        sm.start_window("test", 0, 900)
        
        # Trigger
        sm.process_tick(make_tick(0, 90, 50), 100)
        
        # Validation window: min=89, max=95 -> PASS
        for i in range(11):  # 10+ seconds
            price = 89 + (i % 7)  # Varies 89-95
            sm.process_tick(make_tick(i + 1, price, 50), 100)
        
        # Should be in entry pending or position
        assert sm.state in [State.ENTRY_PENDING, State.IN_POSITION]
    
    def test_spike_fail_min(self):
        """Min below 88 should fail."""
        cfg = StrategyConfig()
        sm = StrategyStateMachine(cfg)
        sm.start_window("test", 0, 900)
        
        # Trigger
        sm.process_tick(make_tick(0, 90, 50), 100)
        
        # Validation window: drops to 85 -> FAIL
        for i in range(11):
            price = 85 + i  # 85, 86, ... 95 (min is 85 < 88)
            result = sm.process_tick(make_tick(i + 1, price, 50), 100)
        
        assert sm.state == State.DONE
        assert sm.context.exit_reason == 'SPIKE_FAIL'
    
    def test_spike_fail_max(self):
        """Max below 93 should fail."""
        cfg = StrategyConfig()
        sm = StrategyStateMachine(cfg)
        sm.start_window("test", 0, 900)
        
        # Trigger
        sm.process_tick(make_tick(0, 90, 50), 100)
        
        # Validation window: stays 89-92 -> FAIL (max < 93)
        for i in range(11):
            price = 89 + (i % 4)  # 89, 90, 91, 92
            result = sm.process_tick(make_tick(i + 1, price, 50), 100)
        
        assert sm.state == State.DONE
        assert sm.context.exit_reason == 'SPIKE_FAIL'


class TestJumpGate:
    """Test JUMP gate (big < 8, mid_count < 2)."""
    
    def test_jump_big_fail(self):
        """Single big jump >= 8 should fail."""
        cfg = StrategyConfig()
        sm = StrategyStateMachine(cfg)
        sm.start_window("test", 0, 900)
        
        # Trigger
        sm.process_tick(make_tick(0, 90, 50), 100)
        
        # Big jump: 90 -> 99 (delta=9)
        sm.process_tick(make_tick(1, 90, 50), 100)
        sm.process_tick(make_tick(2, 99, 42), 100)  # +9 on UP
        
        # Continue to complete validation
        for i in range(3, 12):
            result = sm.process_tick(make_tick(i, 95, 50), 100)
        
        assert sm.state == State.DONE
        assert sm.context.exit_reason == 'JUMP_FAIL'
    
    def test_jump_mid_count_fail(self):
        """Two mid jumps >= 3 should fail."""
        cfg = StrategyConfig()
        sm = StrategyStateMachine(cfg)
        sm.start_window("test", 0, 900)
        
        # Trigger
        sm.process_tick(make_tick(0, 90, 50), 100)
        
        # Two mid jumps
        sm.process_tick(make_tick(1, 90, 50), 100)
        sm.process_tick(make_tick(2, 94, 50), 100)  # +4 (mid jump 1)
        sm.process_tick(make_tick(3, 90, 50), 100)  # -4 (mid jump 2)
        
        # Continue
        for i in range(4, 12):
            result = sm.process_tick(make_tick(i, 93, 50), 100)
        
        assert sm.state == State.DONE
        assert sm.context.exit_reason == 'JUMP_FAIL'


class TestExitLogic:
    """Test TP/SL exits."""
    
    def _setup_in_position(self) -> StrategyStateMachine:
        """Helper to get SM into IN_POSITION state."""
        cfg = StrategyConfig()
        sm = StrategyStateMachine(cfg)
        sm.start_window("test", 0, 900)
        
        # Trigger at 91c
        sm.process_tick(make_tick(0, 91, 50), 100)
        
        # Valid spike (varies 89-95 to pass min>=88, max>=93)
        for i in range(1, 11):
            price = 89 + (i % 7)  # 89, 90, 91, 92, 93, 94, 95
            sm.process_tick(make_tick(i, price, 50), 100)
        
        # After validation, we're ENTRY_PENDING. Need a tick at <= 92c
        # so that fill_price = 92 + 1 slip = 93 <= p_max
        sm.process_tick(make_tick(11, 92, 50), 100)
        
        assert sm.state == State.IN_POSITION, f"Expected IN_POSITION, got {sm.state}"
        return sm
    
    def test_tp_exit(self):
        """Should exit at TP=97."""
        sm = self._setup_in_position()
        
        # Price hits 97
        result = sm.process_tick(make_tick(15, 97, 30), 100)
        
        assert sm.state == State.DONE
        assert sm.context.exit_reason == 'TP'
        assert sm.context.exit_price == 97
        assert sm.context.realized_pnl_invested > 0
    
    def test_sl_exit(self):
        """Should exit at SL=86."""
        sm = self._setup_in_position()
        
        # Price drops to 85
        result = sm.process_tick(make_tick(15, 85, 60), 100)
        
        assert sm.state == State.DONE
        assert sm.context.exit_reason == 'SL'
        assert sm.context.exit_price <= 85  # With slip
        assert sm.context.realized_pnl_invested < 0


class TestSettlement:
    """Test settlement logic."""
    
    def test_settlement_win(self):
        """Should pay 100c on win."""
        cfg = StrategyConfig()
        sm = StrategyStateMachine(cfg)
        sm.start_window("test", 0, 900)
        
        # Trigger at 91c
        sm.process_tick(make_tick(0, 91, 50), 100)
        
        # Valid spike (varies 89-95)
        for i in range(1, 11):
            price = 89 + (i % 7)
            sm.process_tick(make_tick(i, price, 50), 100)
        
        # Fill at 92c (92 + 1 slip = 93 <= p_max)
        sm.process_tick(make_tick(11, 92, 50), 100)
        
        assert sm.state == State.IN_POSITION
        
        # Force settle with UP winning
        result = sm.force_settle('UP')
        
        assert sm.state == State.DONE
        assert sm.context.exit_reason == 'SETTLEMENT_WIN'
        assert sm.context.exit_price == 100
        assert sm.context.realized_pnl_invested > 0
    
    def test_settlement_loss(self):
        """Should pay 0c on loss."""
        cfg = StrategyConfig()
        sm = StrategyStateMachine(cfg)
        sm.start_window("test", 0, 900)
        
        # Trigger at 91c (UP side)
        sm.process_tick(make_tick(0, 91, 50), 100)
        
        # Valid spike (varies 89-95)
        for i in range(1, 11):
            price = 89 + (i % 7)
            sm.process_tick(make_tick(i, price, 50), 100)
        
        # Fill at 92c
        sm.process_tick(make_tick(11, 92, 50), 100)
        
        assert sm.state == State.IN_POSITION
        assert sm.context.trigger_side == 'UP'
        
        # Force settle with DOWN winning
        result = sm.force_settle('DOWN')
        
        assert sm.state == State.DONE
        assert sm.context.exit_reason == 'SETTLEMENT_LOSS'
        assert sm.context.exit_price == 0
        assert sm.context.realized_pnl_invested < 0


class TestInvalidTicks:
    """Test handling of invalid ticks."""
    
    def test_invalid_tick_ignored(self):
        """Ticks with invalid prices should be ignored."""
        sm = StrategyStateMachine(StrategyConfig())
        sm.start_window("test", 0, 900)
        
        # Invalid tick (negative)
        result = sm.process_tick(make_tick(1, -1, 50), 100)
        assert len(sm.context.ticks) == 0  # Not added
        
        # Invalid tick (> 100)
        result = sm.process_tick(make_tick(2, 105, 50), 100)
        assert len(sm.context.ticks) == 0
        
        # Valid tick
        result = sm.process_tick(make_tick(3, 50, 50), 100)
        assert len(sm.context.ticks) == 1

