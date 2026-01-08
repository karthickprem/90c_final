"""
Inventory Tracking
==================
Track positions from fills with reconciliation.
"""

import time
from typing import Dict, Optional, List
from dataclasses import dataclass, field
from threading import RLock

from .config import Config


@dataclass
class Position:
    """Position in a single token"""
    token_id: str
    shares: float = 0.0
    avg_cost: float = 0.0
    total_cost: float = 0.0
    
    # Tracking
    buys: int = 0
    sells: int = 0
    last_update: float = 0.0
    
    def add_buy(self, shares: float, price: float):
        """Record a buy fill"""
        cost = shares * price
        new_total_shares = self.shares + shares
        
        if new_total_shares > 0:
            self.avg_cost = (self.total_cost + cost) / new_total_shares
        
        self.shares = new_total_shares
        self.total_cost += cost
        self.buys += 1
        self.last_update = time.time()
    
    def add_sell(self, shares: float, price: float):
        """Record a sell fill"""
        if shares > self.shares:
            shares = self.shares  # Can't sell more than we have
        
        # Reduce position
        if self.shares > 0:
            cost_basis = (shares / self.shares) * self.total_cost
            self.total_cost -= cost_basis
        
        self.shares -= shares
        self.sells += 1
        self.last_update = time.time()
        
        if self.shares <= 0:
            self.shares = 0
            self.total_cost = 0
            self.avg_cost = 0
    
    @property
    def unrealized_pnl(self) -> float:
        """Unrealized P&L assuming position worth $1 if wins"""
        # For binary markets: max value is 1.0 per share
        return self.shares - self.total_cost


@dataclass
class InventoryState:
    """Overall inventory state"""
    positions: Dict[str, Position] = field(default_factory=dict)
    usdc_available: float = 0.0
    usdc_locked: float = 0.0  # In open orders
    
    # Risk tracking
    total_position_value: float = 0.0
    unrealized_pnl: float = 0.0
    
    # Reconciliation
    last_reconcile: float = 0.0
    reconcile_errors: int = 0


class InventoryManager:
    """
    Manage inventory with:
    - Real-time fill updates
    - Periodic reconciliation with API
    - Risk limit enforcement
    """
    
    def __init__(self, config: Config):
        self.config = config
        self.state = InventoryState()
        self._lock = RLock()  # Reentrant lock to allow nested locking
        
        # Token mapping
        self.yes_token: Optional[str] = None
        self.no_token: Optional[str] = None
    
    def set_tokens(self, yes_token: str, no_token: str):
        """Set token IDs for YES/NO"""
        self.yes_token = yes_token
        self.no_token = no_token
        
        # Initialize positions if not exists
        with self._lock:
            if yes_token not in self.state.positions:
                self.state.positions[yes_token] = Position(token_id=yes_token)
            if no_token not in self.state.positions:
                self.state.positions[no_token] = Position(token_id=no_token)
    
    def process_fill(self, token_id: str, side: str, shares: float, price: float):
        """Process a fill event (from WS or API)"""
        with self._lock:
            if token_id not in self.state.positions:
                self.state.positions[token_id] = Position(token_id=token_id)
            
            pos = self.state.positions[token_id]
            
            if side.upper() == "BUY":
                pos.add_buy(shares, price)
                self.state.usdc_available -= shares * price
            else:
                pos.add_sell(shares, price)
                self.state.usdc_available += shares * price
            
            self._update_totals()
    
    def _update_totals(self):
        """Update total position values"""
        total_value = 0.0
        total_pnl = 0.0
        
        for pos in self.state.positions.values():
            # Value at mid (assume 0.5 for conservative estimate)
            total_value += pos.shares * 0.5
            total_pnl += pos.unrealized_pnl
        
        self.state.total_position_value = total_value
        self.state.unrealized_pnl = total_pnl
    
    def update_locked(self, locked_usdc: float):
        """Update USDC locked in open orders"""
        with self._lock:
            self.state.usdc_locked = locked_usdc
    
    def reconcile(self, usdc_balance: float, position_value: float):
        """Reconcile with API values (basic version)"""
        with self._lock:
            self.state.usdc_available = usdc_balance
            self.state.total_position_value = position_value
            self.state.last_reconcile = time.time()
    
    def reconcile_positions(self, actual_positions: Dict[str, float], verbose: bool = True):
        """
        Full reconciliation of actual positions from REST API.
        
        Args:
            actual_positions: Dict of token_id -> shares from API
            verbose: Whether to log changes
        
        Returns:
            Dict of mismatches found
        """
        mismatches = {}
        
        with self._lock:
            # Check each token we're tracking
            for token_id in [self.yes_token, self.no_token]:
                if not token_id:
                    continue
                
                internal = self.state.positions.get(token_id, Position(token_id=token_id)).shares
                actual = actual_positions.get(token_id, 0.0)
                
                diff = abs(internal - actual)
                if diff > 0.01:  # More than 0.01 share difference
                    mismatches[token_id] = {
                        "internal": internal,
                        "actual": actual,
                        "diff": actual - internal
                    }
                    
                    if verbose:
                        label = "YES" if token_id == self.yes_token else "NO"
                        print(f"[RECONCILE] {label} mismatch: internal={internal:.2f} actual={actual:.2f}", flush=True)
                    
                    # Overwrite internal with actual
                    if token_id not in self.state.positions:
                        self.state.positions[token_id] = Position(token_id=token_id)
                    self.state.positions[token_id].shares = actual
            
            # Check for positions in actual that we don't track
            for token_id, shares in actual_positions.items():
                if token_id not in [self.yes_token, self.no_token]:
                    continue  # Only track our market
                if token_id not in self.state.positions and shares > 0:
                    if verbose:
                        print(f"[RECONCILE] New position found: {token_id[:20]}... = {shares:.2f}", flush=True)
                    self.state.positions[token_id] = Position(token_id=token_id, shares=shares)
                    mismatches[token_id] = {"internal": 0, "actual": shares, "diff": shares}
            
            self.state.last_reconcile = time.time()
            
            if mismatches:
                self.state.reconcile_errors += 1
            
            return mismatches
    
    def force_set_shares(self, token_id: str, shares: float, avg_cost: float = 0.0):
        """Force-set inventory (used after reconciliation finds mismatch)"""
        with self._lock:
            if token_id not in self.state.positions:
                self.state.positions[token_id] = Position(token_id=token_id)
            
            self.state.positions[token_id].shares = shares
            self.state.positions[token_id].avg_cost = avg_cost
            self.state.positions[token_id].total_cost = shares * avg_cost
            self.state.positions[token_id].last_update = time.time()
    
    def get_position(self, token_id: str) -> Position:
        """Get position for a token"""
        with self._lock:
            if token_id not in self.state.positions:
                self.state.positions[token_id] = Position(token_id=token_id)
            return self.state.positions[token_id]
    
    def get_yes_shares(self) -> float:
        """Get YES (UP) token shares"""
        if self.yes_token:
            return self.get_position(self.yes_token).shares
        return 0.0
    
    def get_no_shares(self) -> float:
        """Get NO (DOWN) token shares"""
        if self.no_token:
            return self.get_position(self.no_token).shares
        return 0.0
    
    # Risk checks
    
    def can_buy(self, token_id: str, shares: float, price: float) -> tuple[bool, str]:
        """Check if we can place a buy order"""
        cost = shares * price
        
        with self._lock:
            # Check USDC limit
            new_locked = self.state.usdc_locked + cost
            if new_locked > self.config.risk.max_usdc_locked:
                return False, f"Would exceed max_usdc_locked ({new_locked:.2f} > {self.config.risk.max_usdc_locked})"
            
            # Check inventory limit
            pos = self.state.positions.get(token_id, Position(token_id=token_id))
            new_shares = pos.shares + shares
            if new_shares > self.config.risk.max_inv_shares_per_token:
                return False, f"Would exceed max_inv_shares ({new_shares:.0f} > {self.config.risk.max_inv_shares_per_token})"
            
            return True, ""
    
    def can_sell(self, token_id: str, shares: float) -> tuple[bool, str]:
        """Check if we can place a sell order"""
        with self._lock:
            pos = self.state.positions.get(token_id, Position(token_id=token_id))
            
            if pos.shares < shares:
                return False, f"Insufficient inventory ({pos.shares:.1f} < {shares:.1f})"
            
            return True, ""
    
    def check_kill_switch(self) -> tuple[bool, str]:
        """Check if kill switch should trigger"""
        with self._lock:
            # Check inventory threshold
            total_shares = sum(p.shares for p in self.state.positions.values())
            if total_shares > self.config.risk.kill_switch_inv_threshold:
                return True, f"Inventory exceeds kill threshold ({total_shares:.0f})"
            
            # Check loss threshold
            if self.state.unrealized_pnl < -self.config.risk.kill_switch_loss_threshold:
                return True, f"Loss exceeds kill threshold ({self.state.unrealized_pnl:.2f})"
            
            return False, ""
    
    def get_summary(self) -> Dict:
        """Get inventory summary"""
        with self._lock:
            return {
                "usdc_available": self.state.usdc_available,
                "usdc_locked": self.state.usdc_locked,
                "yes_shares": self.get_yes_shares(),
                "no_shares": self.get_no_shares(),
                "total_position_value": self.state.total_position_value,
                "unrealized_pnl": self.state.unrealized_pnl,
                "last_reconcile": self.state.last_reconcile
            }

