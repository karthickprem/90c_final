"""
Balance & Account Snapshot
==========================
Distinguishes between:
- cash_available_usdc: Spendable USDC (cash icon)
- positions_mtm_usdc: Mark-to-market value of positions
- equity_estimate: cash + positions (portfolio value)

CRITICAL: Bot must ONLY use cash_available_usdc for sizing new orders!
"""

from dataclasses import dataclass
from typing import Optional, Dict
import time


@dataclass
class AccountSnapshot:
    """
    Complete account state snapshot.
    
    IMPORTANT DISTINCTION:
    - portfolio value (UI) = cash + positions MTM
    - spendable cash = cash_available_usdc - locked_in_buys - safety_buffer
    
    The bot must ONLY use spendable cash for new order sizing!
    """
    
    # Cash balances
    cash_available_usdc: float = 0.0  # Spendable USDC (cash icon)
    
    # Locked in orders
    locked_usdc_in_open_buys: float = 0.0  # sum(price * size) for open BUY orders
    
    # Position values
    positions_mtm_usdc: float = 0.0  # Mark-to-market value of all positions
    
    # Computed
    equity_estimate_usdc: float = 0.0  # cash + positions MTM
    spendable_usdc: float = 0.0  # cash - locked - buffer
    
    # Metadata
    timestamp: float = 0.0
    safety_buffer: float = 0.50  # Reserve buffer
    
    def __post_init__(self):
        self.timestamp = time.time()
        self._compute()
    
    def _compute(self):
        """Compute derived values"""
        self.equity_estimate_usdc = self.cash_available_usdc + self.positions_mtm_usdc
        self.spendable_usdc = max(0, self.cash_available_usdc - self.locked_usdc_in_open_buys - self.safety_buffer)
    
    def can_place_order(self, notional: float, min_notional: float = 1.0) -> tuple[bool, str]:
        """
        Check if we can place a new order.
        
        Returns (can_place, reason)
        """
        if notional < min_notional:
            return False, f"Notional ${notional:.2f} < min ${min_notional:.2f}"
        
        if notional > self.spendable_usdc:
            return False, f"Notional ${notional:.2f} > spendable ${self.spendable_usdc:.2f}"
        
        return True, ""
    
    def to_dict(self) -> Dict:
        """Convert to dict for logging"""
        return {
            "cash_available": round(self.cash_available_usdc, 4),
            "locked_in_buys": round(self.locked_usdc_in_open_buys, 4),
            "positions_mtm": round(self.positions_mtm_usdc, 4),
            "equity_estimate": round(self.equity_estimate_usdc, 4),
            "spendable": round(self.spendable_usdc, 4),
            "safety_buffer": self.safety_buffer
        }
    
    def to_log_string(self) -> str:
        """Format for console logging"""
        return (
            f"Cash: ${self.cash_available_usdc:.2f} | "
            f"Locked: ${self.locked_usdc_in_open_buys:.2f} | "
            f"Positions: ${self.positions_mtm_usdc:.2f} | "
            f"Equity: ${self.equity_estimate_usdc:.2f} | "
            f"Spendable: ${self.spendable_usdc:.2f}"
        )


class BalanceManager:
    """
    Manages balance fetching and account snapshots.
    
    CRITICAL: Always use get_snapshot().spendable_usdc for order sizing!
    Never use equity or portfolio value!
    """
    
    def __init__(self, clob, config):
        self.clob = clob
        self.config = config
        self._last_snapshot: Optional[AccountSnapshot] = None
        self._safety_buffer = 0.50  # Reserve $0.50 as safety
        self._min_notional = 1.0  # Polymarket minimum
    
    def get_snapshot(self) -> AccountSnapshot:
        """
        Fetch current account snapshot.
        
        Returns AccountSnapshot with all balance components.
        """
        # Get raw balance from API
        balance = self.clob.get_balance()
        
        # Calculate locked in open buys
        locked = 0.0
        open_orders = self.clob.get_open_orders()
        for order in open_orders:
            if order.side == "BUY":
                # Locked = price * remaining size
                remaining = order.size - order.size_matched
                locked += order.price * remaining
        
        snapshot = AccountSnapshot(
            cash_available_usdc=balance["usdc"],
            locked_usdc_in_open_buys=locked,
            positions_mtm_usdc=balance["positions"],
            safety_buffer=self._safety_buffer
        )
        
        self._last_snapshot = snapshot
        return snapshot
    
    def can_place_buy(
        self,
        price: float,
        size: float,
        snapshot: Optional[AccountSnapshot] = None
    ) -> tuple[bool, str, float]:
        """
        Check if we can place a buy order.
        
        Returns (can_place, reason, adjusted_size)
        - If can_place=False, reason explains why
        - adjusted_size may be reduced to fit constraints
        """
        if snapshot is None:
            snapshot = self.get_snapshot()
        
        notional = price * size
        
        # Check minimum notional
        if notional < self._min_notional:
            required_size = int(self._min_notional / price) + 1
            return False, f"SKIP_MIN_NOTIONAL: price={price:.2f}, need size={required_size}, have size={size}", 0
        
        # Check spendable
        if notional > snapshot.spendable_usdc:
            # Try to reduce size
            max_size = int(snapshot.spendable_usdc / price)
            if max_size * price >= self._min_notional:
                return True, f"Reduced size from {size} to {max_size}", max_size
            else:
                return False, f"Insufficient spendable: ${snapshot.spendable_usdc:.2f} < ${notional:.2f}", 0
        
        return True, "", size
    
    def get_required_size_for_min_notional(self, price: float) -> int:
        """Get minimum size needed to meet $1 notional"""
        if price <= 0:
            return 0
        return int(self._min_notional / price) + 1
    
    @property
    def min_notional(self) -> float:
        return self._min_notional
    
    @property
    def safety_buffer(self) -> float:
        return self._safety_buffer

