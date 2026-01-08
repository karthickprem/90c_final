"""
Order Manager
=============
Manage order lifecycle: create, replace, cancel.
Prevents duplicates and handles partial fills.
"""

import time
from typing import Dict, Optional, List
from dataclasses import dataclass, field
from threading import Lock

from .config import Config
from .clob import ClobWrapper, Side, OrderResult, OpenOrder
from .quoting import Quote


class OrderRole:
    ENTRY = "ENTRY"
    EXIT = "EXIT"


@dataclass
class ManagedOrder:
    """An order being managed"""
    order_id: str
    token_id: str
    side: str
    price: float
    size: float
    size_matched: float = 0.0
    status: str = "PENDING"
    role: str = OrderRole.ENTRY  # ENTRY or EXIT
    created_at: float = field(default_factory=time.time)
    last_update: float = field(default_factory=time.time)
    
    @property
    def size_remaining(self) -> float:
        return max(0, self.size - self.size_matched)
    
    @property
    def is_active(self) -> bool:
        return self.status in ["PENDING", "OPEN", "LIVE"]


class OrderManager:
    """
    Manage orders with:
    - Duplicate prevention
    - Replace logic with throttling
    - Partial fill handling
    - Kill switch support
    """
    
    def __init__(self, config: Config, clob: ClobWrapper):
        self.config = config
        self.clob = clob
        self._lock = Lock()
        
        # Active orders: token_id -> side -> ManagedOrder
        self._orders: Dict[str, Dict[str, ManagedOrder]] = {}
        
        # Metrics
        self._total_orders = 0
        self._total_cancels = 0
        self._total_replaces = 0
        self._replace_rejects = 0
        
        # Timing
        self._last_update: Dict[str, float] = {}  # token_side -> timestamp
    
    def _get_key(self, token_id: str, side: str) -> str:
        return f"{token_id}_{side}"
    
    def get_order(self, token_id: str, side: str) -> Optional[ManagedOrder]:
        """Get active order for token/side"""
        with self._lock:
            if token_id in self._orders:
                return self._orders[token_id].get(side)
            return None
    
    def get_all_orders(self) -> List[ManagedOrder]:
        """Get all active orders"""
        with self._lock:
            result = []
            for token_orders in self._orders.values():
                for order in token_orders.values():
                    if order.is_active:
                        result.append(order)
            return result
    
    def place_or_replace(
        self,
        token_id: str,
        quote: Quote,
        role: str = OrderRole.ENTRY
    ) -> Optional[OrderResult]:
        """
        Place new order or replace existing if price changed.
        
        Enforces:
        - Max 1 order per token per side
        - Minimum update interval
        - Replace throttling
        """
        side = quote.side
        key = self._get_key(token_id, side)
        
        with self._lock:
            # Check update interval
            last_update = self._last_update.get(key, 0)
            if time.time() - last_update < self.config.risk.min_update_interval:
                return None  # Too soon
            
            # Check existing order
            existing = None
            if token_id in self._orders:
                existing = self._orders[token_id].get(side)
            
            if existing and existing.is_active:
                # Don't replace EXIT orders during normal churn (only reprice explicitly)
                if existing.role == OrderRole.EXIT and role == OrderRole.ENTRY:
                    return None  # Don't overwrite exit with entry
                
                # Check if replace is needed
                price_change = abs(existing.price - quote.price)
                if price_change < self.config.quoting.tick_size:
                    return None  # No change needed
                
                # Cancel existing first
                if not self._cancel_internal(existing.order_id):
                    return None
                
                self._total_replaces += 1
            
            # Place new order
            clob_side = Side.BUY if side == "BUY" else Side.SELL
            result = self.clob.post_order(
                token_id=token_id,
                side=clob_side,
                price=quote.price,
                size=quote.size,
                post_only=True  # CRITICAL: maker only
            )
            
            if result.success:
                # Track order
                order = ManagedOrder(
                    order_id=result.order_id or f"UNKNOWN_{time.time()}",
                    token_id=token_id,
                    side=side,
                    price=quote.price,
                    size=quote.size,
                    status="OPEN",
                    role=role
                )
                
                if token_id not in self._orders:
                    self._orders[token_id] = {}
                self._orders[token_id][side] = order
                
                self._total_orders += 1
                self._last_update[key] = time.time()
            
            elif result.would_cross:
                # Post-only rejection
                self._replace_rejects += 1
                if self.config.verbose:
                    print(f"[OM] Post-only rejected: {side} @ {quote.price}")
            
            return result
    
    def _cancel_internal(self, order_id: str) -> bool:
        """Internal cancel (already holding lock)"""
        success = self.clob.cancel_order(order_id)
        if success:
            self._total_cancels += 1
        return success
    
    def cancel(self, token_id: str, side: str) -> bool:
        """Cancel order for token/side"""
        with self._lock:
            if token_id not in self._orders:
                return True
            
            order = self._orders[token_id].get(side)
            if not order or not order.is_active:
                return True
            
            success = self._cancel_internal(order.order_id)
            if success:
                order.status = "CANCELLED"
            
            return success
    
    def cancel_all(self) -> bool:
        """Cancel all orders - KILL SWITCH"""
        print("[OM] KILL SWITCH - CANCELLING ALL ORDERS")
        
        with self._lock:
            success = self.clob.cancel_all()
            
            # Mark all as cancelled
            for token_orders in self._orders.values():
                for order in token_orders.values():
                    order.status = "CANCELLED"
            
            return success
    
    def update_from_fill(self, order_id: str, size_matched: float):
        """Update order with fill information"""
        with self._lock:
            for token_orders in self._orders.values():
                for order in token_orders.values():
                    if order.order_id == order_id:
                        order.size_matched = size_matched
                        order.last_update = time.time()
                        
                        if order.size_remaining <= 0:
                            order.status = "FILLED"
                        
                        return
    
    def sync_with_api(self, api_orders: List[OpenOrder]):
        """Sync internal state with API orders"""
        with self._lock:
            api_order_ids = {o.order_id for o in api_orders}
            
            # Mark orders not in API as cancelled
            for token_orders in self._orders.values():
                for order in token_orders.values():
                    if order.is_active and order.order_id not in api_order_ids:
                        order.status = "CANCELLED"
            
            # Update existing orders
            for api_order in api_orders:
                for token_orders in self._orders.values():
                    for order in token_orders.values():
                        if order.order_id == api_order.order_id:
                            order.size_matched = api_order.size_matched
                            order.status = api_order.status
    
    def get_locked_usdc(self) -> float:
        """Get total USDC locked in open buy orders"""
        with self._lock:
            total = 0.0
            for token_orders in self._orders.values():
                buy_order = token_orders.get("BUY")
                if buy_order and buy_order.is_active:
                    total += buy_order.size_remaining * buy_order.price
            return total
    
    def has_exit_order(self, token_id: str) -> bool:
        """Check if an active exit order exists for token"""
        with self._lock:
            if token_id not in self._orders:
                return False
            order = self._orders[token_id].get("SELL")
            return order is not None and order.is_active and order.role == OrderRole.EXIT
    
    def get_exit_order(self, token_id: str) -> Optional[ManagedOrder]:
        """Get the exit order for a token"""
        with self._lock:
            if token_id not in self._orders:
                return None
            order = self._orders[token_id].get("SELL")
            if order and order.is_active and order.role == OrderRole.EXIT:
                return order
            return None
    
    def get_metrics(self) -> Dict:
        """Get order manager metrics"""
        with self._lock:
            active_count = sum(
                1 for to in self._orders.values()
                for o in to.values()
                if o.is_active
            )
            
            return {
                "active_orders": active_count,
                "total_orders": self._total_orders,
                "total_cancels": self._total_cancels,
                "total_replaces": self._total_replaces,
                "replace_rejects": self._replace_rejects,
                "locked_usdc": self.get_locked_usdc()
            }

