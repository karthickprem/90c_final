"""
Exit Supervisor - PRIORITY 1
============================
Ensures exits ALWAYS happen when inventory exists.

Key invariants:
1. If inv > 0, there MUST be an exit order (or one is placed immediately)
2. Exit orders are NEVER cancelled by kill switch
3. Stop-loss triggers aggressive repricing
4. Emergency taker exit is available when configured

FIXES (from user feedback):
- Emergency is now a SINGLE STATE per token (no spam)
- Balance/allowance errors trigger position refresh + size clamp
- Reprice at most every 1-2s in emergency mode
"""

import time
from dataclasses import dataclass, field
from typing import Optional, Dict
from enum import Enum


class ExitMode(Enum):
    NORMAL = "normal"           # Try maker exit
    AGGRESSIVE = "aggressive"   # Reprice every 1s
    EMERGENCY = "emergency"     # Cross spread if needed


@dataclass
class ExitOrder:
    """Tracks an active exit order"""
    order_id: str
    token_id: str
    shares: float
    price: float
    created_at: float
    last_reprice: float = 0.0
    mode: ExitMode = ExitMode.NORMAL
    reprice_failures: int = 0  # Track consecutive failures
    
    @property
    def age_seconds(self) -> float:
        return time.time() - self.created_at
    
    @property
    def time_since_reprice(self) -> float:
        if self.last_reprice == 0:
            return self.age_seconds
        return time.time() - self.last_reprice


@dataclass
class EmergencyState:
    """Per-token emergency state - prevents spam"""
    token_id: str
    entered_at: float
    last_action: float = 0.0
    retry_count: int = 0
    max_retries: int = 10
    
    @property
    def should_act(self) -> bool:
        """Only act every 1-2 seconds in emergency"""
        return time.time() - self.last_action >= 1.5
    
    def record_action(self):
        self.last_action = time.time()
        self.retry_count += 1
    
    @property
    def exhausted(self) -> bool:
        return self.retry_count >= self.max_retries


class ExitSupervisor:
    """
    Ensures inventory is always exited properly.
    
    Runs EVERY loop tick regardless of:
    - Cooldown
    - Spike pause
    - Kill switch (except for catastrophic failure)
    
    Exit orders are PROTECTED - never cancelled by normal kill events.
    
    EMERGENCY MODE:
    - Is a SINGLE STATE per token (entered once, not spammed)
    - Acts at most every 1-2 seconds
    - Has a max retry count before giving up
    """
    
    def __init__(self, config, clob, position_manager, order_manager):
        self.config = config
        self.clob = clob
        self.position_manager = position_manager
        self.order_manager = order_manager
        
        # Active exit orders by token_id
        self._exit_orders: Dict[str, ExitOrder] = {}
        
        # Emergency states by token_id (prevents spam)
        self._emergency_states: Dict[str, EmergencyState] = {}
        
        # Config - CONSERVATIVE: Do NOT rely on rebates as safety buffer!
        # Rebates are bonus, not guaranteed. Break-even on TRADE P&L.
        self.stop_loss_cents = getattr(config.risk, 'stop_loss_cents', 2)  # 2c = aggressive exit
        self.emergency_taker_threshold_cents = 3  # 3c = emergency, must exit NOW
        self.emergency_taker_enabled = getattr(config.risk, 'emergency_taker_exit', False)
        
        # Repricing schedule - OPTION A: HFT-like fast repricing
        self.reprice_normal_secs = 0.5    # Was 3s, now 500ms (6x faster)
        self.reprice_aggressive_secs = 0.25  # Was 1s, now 250ms (4x faster)
        self.emergency_after_secs = 5.0   # Was 10s, now 5s (2x faster)
        
        # Exit price ladder - OPTION A: Faster (HFT-like)
        # T=0-3s:   try entry price (scratch)
        # T=3-8s:   accept entry-1c
        # T=8-15s:  accept entry-2c
        # T=15s+:   EMERGENCY flatten (was 40s)
        
        # Metrics
        self.exits_placed = 0
        self.exits_repriced = 0
        self.exits_filled = 0
        self.emergency_exits = 0
        self.balance_errors = 0
    
    def tick(self, yes_book: dict, no_book: dict, yes_token: str, no_token: str) -> None:
        """
        Run exit supervision. Call this EVERY loop tick.
        
        Args:
            yes_book: {"best_bid": float, "best_ask": float}
            no_book: {"best_bid": float, "best_ask": float}
        """
        # Check YES position
        yes_pos = self.position_manager.get_position(yes_token)
        if yes_pos and yes_pos.shares > 0:
            self._ensure_exit(yes_token, yes_pos.shares, yes_pos.entry_price, yes_book)
        
        # Check NO position
        no_pos = self.position_manager.get_position(no_token)
        if no_pos and no_pos.shares > 0:
            self._ensure_exit(no_token, no_pos.shares, no_pos.entry_price, no_book)
    
    def _ensure_exit(self, token_id: str, shares: float, entry_price: float, book: dict) -> None:
        """Ensure an exit order exists for this position"""
        best_bid = book.get("best_bid", 0)
        best_ask = book.get("best_ask", 0)
        
        if best_bid <= 0.01:
            print(f"[EXIT] No valid book for {token_id[:20]}..., cannot exit", flush=True)
            return
        
        # Check if in emergency state for this token
        emergency_state = self._emergency_states.get(token_id)
        if emergency_state:
            # Already in emergency - check if we should act
            if not emergency_state.should_act:
                return  # Wait before next action
            if emergency_state.exhausted:
                print(f"[EXIT] EMERGENCY exhausted for {token_id[:20]}..., manual intervention needed", flush=True)
                return
        
        # Check adverse move (cents below entry) - CONSERVATIVE approach
        adverse = entry_price - best_bid if entry_price > 0 else 0
        adverse_cents = adverse * 100
        
        # Determine exit mode - do NOT rely on rebates as buffer!
        # 2c loss = go AGGRESSIVE (start tightening exits)
        # 3c loss = EMERGENCY (must exit, we're losing real money)
        mode = ExitMode.NORMAL
        if adverse_cents >= self.emergency_taker_threshold_cents:
            mode = ExitMode.EMERGENCY
            if not emergency_state:
                print(f"[EXIT] EMERGENCY: entry={entry_price:.4f} bid={best_bid:.4f} loss={adverse_cents:.1f}c", flush=True)
        elif adverse_cents >= self.stop_loss_cents:
            mode = ExitMode.AGGRESSIVE
            if not emergency_state:
                print(f"[EXIT] AGGRESSIVE: entry={entry_price:.4f} bid={best_bid:.4f} loss={adverse_cents:.1f}c", flush=True)
        
        # Check existing exit order
        existing = self._exit_orders.get(token_id)
        
        if existing:
            self._maybe_reprice(existing, best_bid, best_ask, mode, shares)
        else:
            self._place_exit(token_id, shares, best_bid, best_ask, mode)
    
    def _place_exit(self, token_id: str, shares: float, best_bid: float, best_ask: float, mode: ExitMode) -> None:
        """Place a new exit order"""
        # Initial price: best_ask - 1 tick (maker, near top)
        tick = 0.01
        price = best_ask - tick
        
        # Clamp to valid range
        price = max(0.01, min(0.99, price))
        
        try:
            result = self.clob.post_order(
                token_id=token_id,
                side="SELL",
                price=price,
                size=shares,
                post_only=True
            )
            
            if result.success and result.order_id:
                self._exit_orders[token_id] = ExitOrder(
                    order_id=result.order_id,
                    token_id=token_id,
                    shares=shares,
                    price=price,
                    created_at=time.time(),
                    mode=mode
                )
                self.exits_placed += 1
                print(f"[EXIT] POSTED: {token_id[:20]}... {shares} @ {price:.4f}", flush=True)
            else:
                print(f"[EXIT] Failed to place exit order: {result.error}", flush=True)
        
        except Exception as e:
            print(f"[EXIT] Error placing exit: {e}", flush=True)
    
    def _maybe_reprice(self, order: ExitOrder, best_bid: float, best_ask: float, mode: ExitMode, actual_shares: float = None) -> None:
        """Check if exit order needs repricing"""
        tick = 0.01
        
        # Use actual shares from position manager if provided (source of truth)
        if actual_shares is not None and actual_shares > 0:
            order.shares = actual_shares
        
        # Update mode if stop-loss triggered
        if mode == ExitMode.AGGRESSIVE and order.mode == ExitMode.NORMAL:
            order.mode = ExitMode.AGGRESSIVE
        
        # Check if emergency mode triggered
        if order.age_seconds > self.emergency_after_secs and self.emergency_taker_enabled:
            if order.mode != ExitMode.EMERGENCY:
                order.mode = ExitMode.EMERGENCY
                # Enter emergency state (single trigger, not spam)
                if order.token_id not in self._emergency_states:
                    self._emergency_states[order.token_id] = EmergencyState(
                        token_id=order.token_id,
                        entered_at=time.time()
                    )
                    print(f"[EXIT] ENTERING EMERGENCY MODE for {order.token_id[:20]}...", flush=True)
        
        # Determine reprice interval
        if order.mode == ExitMode.EMERGENCY:
            reprice_interval = 1.5  # Slower to prevent spam
        elif order.mode == ExitMode.AGGRESSIVE:
            reprice_interval = self.reprice_aggressive_secs
        else:
            reprice_interval = self.reprice_normal_secs
        
        # Check if time to reprice
        if order.time_since_reprice < reprice_interval:
            return
        
        # Calculate new price - OPTION A: Fast ladder (HFT-like)
        # Goal: exit quickly, break-even on trade P&L
        if order.mode == ExitMode.EMERGENCY:
            # Cross the spread - must exit NOW
            new_price = best_bid  # Will hit bids as taker
            post_only = not self.emergency_taker_enabled
        elif order.age_seconds > 8.0:
            # After 8s: accept entry-2c (aggressive exit)
            new_price = best_bid + tick
        elif order.age_seconds > 3.0:
            # After 3s: accept entry-1c
            new_price = best_ask
        else:
            # First 3s: try entry price or better (scratch exit)
            new_price = best_ask - tick
        
        new_price = max(0.01, min(0.99, new_price))
        
        # Check if price changed enough
        if abs(new_price - order.price) < tick / 2:
            return
        
        # Cancel old and place new
        try:
            self.clob.cancel_order(order.order_id)
            
            result = self.clob.post_order(
                token_id=order.token_id,
                side="SELL",
                price=new_price,
                size=order.shares,
                post_only=not (order.mode == ExitMode.EMERGENCY and self.emergency_taker_enabled)
            )
            
            if result.success and result.order_id:
                order.order_id = result.order_id
                order.price = new_price
                order.last_reprice = time.time()
                order.reprice_failures = 0
                self.exits_repriced += 1
                
                # Record action in emergency state
                if order.token_id in self._emergency_states:
                    self._emergency_states[order.token_id].record_action()
                
                mode_str = order.mode.value
                print(f"[EXIT] REPRICED ({mode_str}): {order.token_id[:20]}... @ {new_price:.4f}", flush=True)
                
                if order.mode == ExitMode.EMERGENCY:
                    self.emergency_exits += 1
            else:
                order.reprice_failures += 1
                self.balance_errors += 1
                
                # Handle balance/allowance error
                if "balance" in str(result.error).lower() or "allowance" in str(result.error).lower():
                    # Refresh positions and clamp size
                    if self.position_manager:
                        self.position_manager.reconcile_from_rest()
                        actual = self.position_manager.get_shares(order.token_id)
                        if actual > 0 and actual < order.shares:
                            print(f"[EXIT] Clamping size from {order.shares:.2f} to {actual:.2f}", flush=True)
                            order.shares = actual
                    
                    # Record in emergency state
                    if order.token_id in self._emergency_states:
                        self._emergency_states[order.token_id].record_action()
                
                if order.reprice_failures < 3:
                    print(f"[EXIT] Reprice failed (will retry): {result.error}", flush=True)
        
        except Exception as e:
            print(f"[EXIT] Error repricing: {e}", flush=True)
    
    def on_fill(self, token_id: str, side: str, shares: float) -> None:
        """Handle fill event"""
        if side == "SELL" and token_id in self._exit_orders:
            order = self._exit_orders[token_id]
            order.shares -= shares
            
            if order.shares <= 0:
                del self._exit_orders[token_id]
                self.exits_filled += 1
                
                # Clear emergency state on successful exit
                if token_id in self._emergency_states:
                    del self._emergency_states[token_id]
                
                print(f"[EXIT] FILLED: {token_id[:20]}...", flush=True)
    
    def clear_token_state(self, token_id: str):
        """Clear all state for a token (call when position goes to 0)"""
        if token_id in self._exit_orders:
            del self._exit_orders[token_id]
        if token_id in self._emergency_states:
            del self._emergency_states[token_id]
    
    def cancel_exit(self, token_id: str) -> None:
        """Cancel exit order - ONLY call when position is confirmed 0"""
        if token_id in self._exit_orders:
            try:
                self.clob.cancel_order(self._exit_orders[token_id].order_id)
            except:
                pass
            del self._exit_orders[token_id]
    
    def get_exit_order(self, token_id: str) -> Optional[ExitOrder]:
        """Get active exit order for token"""
        return self._exit_orders.get(token_id)
    
    def has_exit_order(self, token_id: str) -> bool:
        """Check if exit order exists"""
        return token_id in self._exit_orders
    
    @property
    def active_exit_count(self) -> int:
        return len(self._exit_orders)
    
    @property
    def tokens_in_emergency(self) -> int:
        return len(self._emergency_states)
    
    def get_metrics(self) -> Dict:
        return {
            "exits_placed": self.exits_placed,
            "exits_repriced": self.exits_repriced,
            "exits_filled": self.exits_filled,
            "emergency_exits": self.emergency_exits,
            "balance_errors": self.balance_errors,
            "active_exits": self.active_exit_count,
            "tokens_in_emergency": self.tokens_in_emergency
        }

