"""
Position Tracking - Source of Truth
====================================
Accurate position tracking by token_id with REST reconciliation.

This is PRIORITY 0 - correctness. The bot cannot be safe if position
tracking is wrong.
"""

import time
import requests
from dataclasses import dataclass, field
from typing import Dict, Optional, List, Set
from threading import RLock


@dataclass
class TokenPosition:
    """Position in a single token"""
    token_id: str
    shares: float = 0.0
    avg_price: float = 0.0
    
    # From REST API
    rest_shares: float = 0.0
    rest_avg_price: float = 0.0
    rest_last_update: float = 0.0
    
    # Computed MTM
    current_bid: float = 0.0
    current_ask: float = 0.0
    mtm_value: float = 0.0  # shares * current_bid (conservative)
    
    # Entry tracking for stop-loss
    entry_price: float = 0.0
    entry_time: float = 0.0
    
    def update_from_rest(self, shares: float, avg_price: float):
        """Update from REST API"""
        self.rest_shares = shares
        self.rest_avg_price = avg_price
        self.rest_last_update = time.time()
        
        # REST is source of truth - overwrite internal
        if abs(self.shares - shares) > 0.01:
            print(f"[POSITIONS] RECONCILE: {self.token_id[:20]}... internal={self.shares:.2f} REST={shares:.2f}", flush=True)
        
        self.shares = shares
        self.avg_price = avg_price if avg_price > 0 else self.avg_price
        
        if shares > 0 and self.entry_price == 0:
            self.entry_price = avg_price
            self.entry_time = time.time()
    
    def update_mtm(self, bid: float, ask: float):
        """Update mark-to-market using current book"""
        self.current_bid = bid
        self.current_ask = ask
        self.mtm_value = self.shares * bid  # Conservative: use bid
    
    def process_fill(self, side: str, shares: float, price: float):
        """Process a fill event"""
        if side == "BUY":
            # Update average price
            total_cost = self.shares * self.avg_price + shares * price
            self.shares += shares
            if self.shares > 0:
                self.avg_price = total_cost / self.shares
            
            # Track entry for stop-loss
            if self.entry_price == 0:
                self.entry_price = price
                self.entry_time = time.time()
        
        elif side == "SELL":
            self.shares = max(0, self.shares - shares)
            if self.shares == 0:
                self.entry_price = 0
                self.entry_time = 0
    
    @property
    def adverse_excursion(self) -> float:
        """How much price has moved against us (negative = favorable)"""
        if self.shares <= 0 or self.entry_price == 0:
            return 0.0
        return self.entry_price - self.current_bid  # Positive = adverse
    
    @property
    def hold_time_seconds(self) -> float:
        """How long we've held this position"""
        if self.entry_time == 0:
            return 0.0
        return time.time() - self.entry_time


class PositionManager:
    """
    Manages positions with REST as source of truth.
    
    Key invariants:
    1. positions_by_token is the source of truth for shares
    2. MTM is computed using current best_bid (conservative)
    3. Mismatch between internal and REST triggers RECONCILE
    """
    
    def __init__(self, config):
        self.config = config
        self._lock = RLock()
        
        # Positions by token_id
        self._positions: Dict[str, TokenPosition] = {}
        
        # Configured market tokens (only track these)
        self._market_tokens: Set[str] = set()
        
        # REST API config
        self._data_api = "https://data-api.polymarket.com"
        self._proxy_address = config.api.proxy_address
        
        # Reconciliation tracking
        self._last_reconcile = 0.0
        self._reconcile_interval = 5.0  # 5 seconds
        self._reconcile_mismatches = 0
    
    def set_market_tokens(self, yes_token: str, no_token: str):
        """Set the tokens for current market"""
        with self._lock:
            self._market_tokens = {yes_token, no_token}
            
            # Initialize positions
            if yes_token not in self._positions:
                self._positions[yes_token] = TokenPosition(token_id=yes_token)
            if no_token not in self._positions:
                self._positions[no_token] = TokenPosition(token_id=no_token)
    
    def reconcile_from_rest(self) -> Dict[str, dict]:
        """
        Fetch actual positions from REST API and reconcile.
        Returns dict of mismatches found.
        """
        mismatches = {}
        
        try:
            r = requests.get(
                f"{self._data_api}/positions",
                params={"user": self._proxy_address},
                timeout=10
            )
            
            if r.status_code != 200:
                print(f"[POSITIONS] REST error: {r.status_code}", flush=True)
                return mismatches
            
            rest_positions = r.json()
            
            with self._lock:
                # Build map of REST positions
                rest_by_token = {}
                for p in rest_positions:
                    token_id = p.get("asset", "")
                    if token_id in self._market_tokens:
                        rest_by_token[token_id] = {
                            "shares": float(p.get("size", 0)),
                            "avg_price": float(p.get("avgPrice", 0))
                        }
                
                # Update each tracked token
                for token_id in self._market_tokens:
                    if token_id not in self._positions:
                        self._positions[token_id] = TokenPosition(token_id=token_id)
                    
                    pos = self._positions[token_id]
                    rest_data = rest_by_token.get(token_id, {"shares": 0, "avg_price": 0})
                    
                    # Check for mismatch
                    internal_shares = pos.shares
                    rest_shares = rest_data["shares"]
                    
                    if abs(internal_shares - rest_shares) > 0.01:
                        mismatches[token_id] = {
                            "internal": internal_shares,
                            "rest": rest_shares,
                            "diff": rest_shares - internal_shares
                        }
                        self._reconcile_mismatches += 1
                    
                    # Update from REST (source of truth)
                    pos.update_from_rest(rest_shares, rest_data["avg_price"])
                
                self._last_reconcile = time.time()
        
        except Exception as e:
            print(f"[POSITIONS] REST fetch error: {e}", flush=True)
        
        return mismatches
    
    def update_mtm(self, token_id: str, bid: float, ask: float):
        """Update MTM for a token from current book"""
        with self._lock:
            if token_id in self._positions:
                self._positions[token_id].update_mtm(bid, ask)
    
    def process_fill(self, token_id: str, side: str, shares: float, price: float):
        """Process a fill event"""
        with self._lock:
            if token_id not in self._positions:
                self._positions[token_id] = TokenPosition(token_id=token_id)
            self._positions[token_id].process_fill(side, shares, price)
    
    def get_position(self, token_id: str) -> Optional[TokenPosition]:
        """Get position for a token"""
        with self._lock:
            return self._positions.get(token_id)
    
    def get_shares(self, token_id: str) -> float:
        """Get shares for a token"""
        with self._lock:
            pos = self._positions.get(token_id)
            return pos.shares if pos else 0.0
    
    def get_total_shares(self) -> float:
        """Get total shares across all tracked tokens"""
        with self._lock:
            return sum(p.shares for p in self._positions.values() if p.token_id in self._market_tokens)
    
    def get_total_mtm(self) -> float:
        """Get total MTM value across all tracked tokens"""
        with self._lock:
            return sum(p.mtm_value for p in self._positions.values() if p.token_id in self._market_tokens)
    
    def get_max_adverse_excursion(self) -> float:
        """Get maximum adverse excursion across all positions"""
        with self._lock:
            excursions = [p.adverse_excursion for p in self._positions.values() if p.shares > 0]
            return max(excursions) if excursions else 0.0
    
    def get_inventory_age_seconds(self) -> float:
        """Get age of oldest position"""
        with self._lock:
            ages = [p.hold_time_seconds for p in self._positions.values() if p.shares > 0]
            return max(ages) if ages else 0.0
    
    def needs_reconcile(self) -> bool:
        """Check if reconciliation is due"""
        return time.time() - self._last_reconcile >= self._reconcile_interval
    
    def get_snapshot(self) -> Dict:
        """Get full position snapshot for logging"""
        with self._lock:
            snapshot = {
                "last_reconcile": self._last_reconcile,
                "mismatches": self._reconcile_mismatches,
                "positions": {}
            }
            
            for token_id, pos in self._positions.items():
                if token_id in self._market_tokens:
                    label = "YES" if list(self._market_tokens).index(token_id) == 0 else "NO"
                    snapshot["positions"][label] = {
                        "shares": pos.shares,
                        "avg_price": pos.avg_price,
                        "current_bid": pos.current_bid,
                        "mtm": pos.mtm_value,
                        "entry_price": pos.entry_price,
                        "adverse_excursion": pos.adverse_excursion,
                        "hold_time_s": pos.hold_time_seconds
                    }
            
            return snapshot

