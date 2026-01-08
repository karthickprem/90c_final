"""
Risk management and kill switch.
Enforces position limits and safety checks.
"""

import logging
from typing import List, Optional, Set
from datetime import datetime, date
from dataclasses import dataclass, field
from enum import Enum

logger = logging.getLogger(__name__)


class RiskViolation(Enum):
    """Types of risk violations that can trigger alerts or kill switch."""
    MAX_DAILY_RISK = "max_daily_risk_exceeded"
    MAX_TOTAL_RISK = "max_total_risk_exceeded"
    SLIPPAGE_EXCEEDED = "slippage_exceeded"
    API_ERROR_SPIKE = "api_error_spike"
    MARKET_SUSPENDED = "market_suspended"
    KILL_SWITCH_ACTIVE = "kill_switch_active"


@dataclass
class RiskState:
    """Current risk state of the bot."""
    total_open_risk: float = 0.0
    daily_risk: float = 0.0
    daily_pnl: float = 0.0
    open_position_count: int = 0
    api_error_count: int = 0
    last_error_time: Optional[datetime] = None
    violations: List[RiskViolation] = field(default_factory=list)
    kill_switch_active: bool = False
    kill_switch_reason: Optional[str] = None
    
    def reset_daily(self):
        """Reset daily counters."""
        self.daily_risk = 0.0
        self.daily_pnl = 0.0
        self.api_error_count = 0
        self.violations = []


@dataclass
class Position:
    """Represents an open position."""
    position_id: str
    target_date: date
    interval_tmin: float
    interval_tmax: float
    total_cost: float
    payout_if_hit: float
    entry_time: datetime
    token_ids: List[str]
    shares_per_leg: float
    is_closed: bool = False
    realized_pnl: float = 0.0
    
    @property
    def max_loss(self) -> float:
        return self.total_cost
    
    @property
    def profit_if_hit(self) -> float:
        return self.payout_if_hit - self.total_cost


class RiskManager:
    """
    Manages risk limits and enforces trading constraints.
    """
    
    def __init__(self, config: dict):
        self.config = config
        
        # Risk limits from config
        self.max_risk_per_day = config.get("max_risk_per_day_usd", 10)
        self.max_total_open_risk = config.get("max_total_open_risk_usd", 30)
        self.max_slippage_pct = config.get("slippage_cap_pct", 0.005)
        self.max_api_errors = config.get("max_api_errors_before_kill", 5)
        
        # State
        self.state = RiskState()
        self.positions: List[Position] = []
        self.blocked_tokens: Set[str] = set()
        
        # Tracking
        self._last_daily_reset = date.today()
    
    def _check_daily_reset(self):
        """Reset daily counters if it's a new day."""
        today = date.today()
        if today > self._last_daily_reset:
            logger.info(f"New day: resetting daily risk counters")
            self.state.reset_daily()
            self._last_daily_reset = today
    
    def can_trade(self) -> tuple[bool, Optional[str]]:
        """
        Check if trading is currently allowed.
        Returns (can_trade, reason_if_blocked).
        """
        self._check_daily_reset()
        
        # Check kill switch
        if self.state.kill_switch_active:
            return False, f"Kill switch active: {self.state.kill_switch_reason}"
        
        # Check total open risk
        if self.state.total_open_risk >= self.max_total_open_risk:
            return False, f"Total open risk ${self.state.total_open_risk:.2f} >= limit ${self.max_total_open_risk}"
        
        # Check daily risk
        if self.state.daily_risk >= self.max_risk_per_day:
            return False, f"Daily risk ${self.state.daily_risk:.2f} >= limit ${self.max_risk_per_day}"
        
        return True, None
    
    def check_trade(self, cost: float, token_ids: List[str]) -> tuple[bool, Optional[str]]:
        """
        Check if a specific trade is allowed.
        
        Args:
            cost: Total cost of the trade
            token_ids: List of token IDs involved
        
        Returns:
            (allowed, reason_if_blocked)
        """
        can, reason = self.can_trade()
        if not can:
            return False, reason
        
        # Check if adding this trade would exceed limits
        new_total_risk = self.state.total_open_risk + cost
        if new_total_risk > self.max_total_open_risk:
            return False, f"Trade would push total risk to ${new_total_risk:.2f} > ${self.max_total_open_risk}"
        
        new_daily_risk = self.state.daily_risk + cost
        if new_daily_risk > self.max_risk_per_day:
            return False, f"Trade would push daily risk to ${new_daily_risk:.2f} > ${self.max_risk_per_day}"
        
        # Check for blocked tokens
        blocked = set(token_ids) & self.blocked_tokens
        if blocked:
            return False, f"Tokens are blocked: {blocked}"
        
        return True, None
    
    def register_trade(self, position: Position):
        """Register a new position after successful execution."""
        self._check_daily_reset()
        
        self.positions.append(position)
        self.state.total_open_risk += position.max_loss
        self.state.daily_risk += position.max_loss
        self.state.open_position_count += 1
        
        logger.info(f"Registered position: {position.position_id} "
                    f"risk=${position.max_loss:.2f} "
                    f"total_open=${self.state.total_open_risk:.2f}")
    
    def close_position(self, position_id: str, pnl: float, 
                       reason: str = "settled"):
        """Close a position and update risk state."""
        for pos in self.positions:
            if pos.position_id == position_id and not pos.is_closed:
                pos.is_closed = True
                pos.realized_pnl = pnl
                
                self.state.total_open_risk -= pos.max_loss
                self.state.daily_pnl += pnl
                self.state.open_position_count -= 1
                
                logger.info(f"Closed position {position_id}: PnL=${pnl:.2f} ({reason})")
                return
        
        logger.warning(f"Position {position_id} not found or already closed")
    
    def check_slippage(self, expected_price: float, actual_price: float) -> bool:
        """
        Check if slippage is within acceptable limits.
        Returns True if OK, False if exceeded.
        """
        if expected_price <= 0:
            return True
        
        slippage = (actual_price - expected_price) / expected_price
        
        if slippage > self.max_slippage_pct:
            self.state.violations.append(RiskViolation.SLIPPAGE_EXCEEDED)
            logger.warning(f"Slippage {slippage:.4f} exceeds limit {self.max_slippage_pct}")
            return False
        
        return True
    
    def record_api_error(self):
        """Record an API error and check if kill switch should activate."""
        self.state.api_error_count += 1
        self.state.last_error_time = datetime.now()
        
        if self.state.api_error_count >= self.max_api_errors:
            self.activate_kill_switch(
                f"API error count ({self.state.api_error_count}) >= threshold ({self.max_api_errors})"
            )
    
    def record_api_success(self):
        """Record successful API call (could reset error count or decay)."""
        # Simple approach: don't reset, let daily reset handle it
        pass
    
    def activate_kill_switch(self, reason: str):
        """Activate the kill switch - stops all trading."""
        self.state.kill_switch_active = True
        self.state.kill_switch_reason = reason
        self.state.violations.append(RiskViolation.KILL_SWITCH_ACTIVE)
        
        logger.critical(f"KILL SWITCH ACTIVATED: {reason}")
    
    def deactivate_kill_switch(self):
        """Manually deactivate the kill switch."""
        if self.state.kill_switch_active:
            logger.warning("Kill switch deactivated manually")
            self.state.kill_switch_active = False
            self.state.kill_switch_reason = None
    
    def block_token(self, token_id: str, reason: str = ""):
        """Block a specific token from trading."""
        self.blocked_tokens.add(token_id)
        logger.warning(f"Token blocked: {token_id} - {reason}")
    
    def unblock_token(self, token_id: str):
        """Unblock a token."""
        self.blocked_tokens.discard(token_id)
        logger.info(f"Token unblocked: {token_id}")
    
    def get_available_risk(self) -> float:
        """Get remaining risk budget for new trades."""
        self._check_daily_reset()
        
        daily_available = self.max_risk_per_day - self.state.daily_risk
        total_available = self.max_total_open_risk - self.state.total_open_risk
        
        return max(0, min(daily_available, total_available))
    
    def get_open_positions(self) -> List[Position]:
        """Get all open (not closed) positions."""
        return [p for p in self.positions if not p.is_closed]
    
    def get_status(self) -> dict:
        """Get current risk status as a dictionary."""
        return {
            "kill_switch_active": self.state.kill_switch_active,
            "kill_switch_reason": self.state.kill_switch_reason,
            "total_open_risk": self.state.total_open_risk,
            "max_total_risk": self.max_total_open_risk,
            "daily_risk": self.state.daily_risk,
            "max_daily_risk": self.max_risk_per_day,
            "daily_pnl": self.state.daily_pnl,
            "open_positions": self.state.open_position_count,
            "api_errors": self.state.api_error_count,
            "available_risk": self.get_available_risk(),
            "violations": [v.value for v in self.state.violations],
            "blocked_tokens": list(self.blocked_tokens),
        }
    
    def print_status(self):
        """Print current risk status."""
        status = self.get_status()
        print("\n=== RISK STATUS ===")
        print(f"Kill Switch: {'ACTIVE - ' + status['kill_switch_reason'] if status['kill_switch_active'] else 'OK'}")
        print(f"Total Open Risk: ${status['total_open_risk']:.2f} / ${status['max_total_risk']:.2f}")
        print(f"Daily Risk: ${status['daily_risk']:.2f} / ${status['max_daily_risk']:.2f}")
        print(f"Available Risk: ${status['available_risk']:.2f}")
        print(f"Daily P&L: ${status['daily_pnl']:.2f}")
        print(f"Open Positions: {status['open_positions']}")
        print(f"API Errors: {status['api_errors']}")
        if status['violations']:
            print(f"Violations: {status['violations']}")
        if status['blocked_tokens']:
            print(f"Blocked Tokens: {len(status['blocked_tokens'])}")


if __name__ == "__main__":
    # Test risk manager
    import yaml
    
    with open("bot/config.yaml") as f:
        config = yaml.safe_load(f)
    
    rm = RiskManager(config)
    rm.print_status()
    
    # Simulate some trades
    print("\n--- Simulating trade checks ---")
    
    can, reason = rm.check_trade(5.0, ["token1", "token2"])
    print(f"Trade $5: {can} - {reason}")
    
    can, reason = rm.check_trade(15.0, ["token3"])
    print(f"Trade $15: {can} - {reason}")
    
    # Register a position
    from uuid import uuid4
    pos = Position(
        position_id=str(uuid4()),
        target_date=date.today(),
        interval_tmin=50,
        interval_tmax=53,
        total_cost=5.0,
        payout_if_hit=10.0,
        entry_time=datetime.now(),
        token_ids=["token1", "token2"],
        shares_per_leg=5
    )
    rm.register_trade(pos)
    rm.print_status()

