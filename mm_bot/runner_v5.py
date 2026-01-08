"""
Runner V5 - CRITICAL FIXES for Safe Operation
==============================================

ROOT CAUSE ANALYSIS from V4 loss:
1. Kept placing entries while holding inventory (pyramiding)
2. Exit repricing posted 2nd SELL before canceling 1st (balance/allowance errors)
3. Fills detected too late (reconcile), bot thought flat and kept quoting
4. Stop-loss sold at bottom right before reversal

P0 FIXES (must-do):
1. INVENTORY GATING: If ANY inventory > 0, bot enters EXIT_ONLY globally
2. EXIT REPLACE: cancel-confirm-post (never two SELLs open)
3. DUST POLICY: Don't spam exits for sizes < MIN_SHARES

P1 FIXES (strategy):
1. REGIME FILTERS: Only trade in stable middle (0.35 < mid < 0.65)
2. TIME-BASED EXIT LADDER: Replace stop-loss with TP/scratch/flatten schedule
3. PENDING FILL STATE: Don't place more entries while entry order exists
"""

import os
import sys
import time
import json
import signal
import threading
from pathlib import Path
from datetime import datetime
from enum import Enum
from typing import Optional, Dict, List, Tuple, Set
from dataclasses import dataclass, field
from collections import deque

from mm_bot.config import Config, RunMode
from mm_bot.clob import ClobWrapper
from mm_bot.positions import PositionManager
from mm_bot.fill_tracker import FillTracker


class BotState(Enum):
    STARTUP = "startup"
    OPENING = "opening"       # NEW: First 60s of window - special handling
    EXIT_ONLY = "exit_only"   # Have inventory - ONLY manage exits
    QUOTING = "quoting"       # Flat - can place entries (normal mode)
    SHUTDOWN = "shutdown"


# SAFE DEFAULTS for $15-20 account
# Use 5.0 to fit within $2.50 budget at 50c price (5 × $0.50 = $2.50)
MIN_SHARES = 5.0
# V9 FIX: Post MIN_SHARES + 0.5 to avoid dust from partial fills
# Note: Can't use +1 on small accounts (would exceed max_usdc_locked)
MIN_QUOTE_SHARES = 5.5  # Small buffer to ensure remaining > MIN_SHARES
SHARE_STEP = 0.01
MIN_NOTIONAL_USD = 1.0

# OPENING MODE (first 30s of window - data shows 50% leave 0.5 zone by 6s)
# Using 30s to catch the full opening period
OPENING_MODE_SECS = 30      # Duration of opening mode
OPEN_VOL_MAX_CENTS = 22.0   # p75 of opening volatility (data-driven)
OPEN_MIN_SPREAD_CENTS = 2.0 # p25 of opening spread (data-driven)

# Regime filters (NORMAL mode after opening) - WIDENED based on data analysis
# Data shows 43% of time in 0.35-0.65, widening to 0.30-0.70 for more opportunities
ENTRY_MID_MIN = 0.30  # Only enter if mid > 30%
ENTRY_MID_MAX = 0.70  # Only enter if mid < 70%
VOL_10S_CENTS = 12.0  # p50 of rest volatility (data-driven)
MIN_SPREAD_CENTS = 1.0  # Lowered from 3.0 - median spread is 2c, don't over-filter

# Time-based exit ladder (replaces stop-loss)
EXIT_TP_CENTS = 2.0       # Target profit: entry + 2c
EXIT_SCRATCH_SECS = 20.0  # After 20s, reprice to entry (scratch)
EXIT_FLATTEN_SECS = 40.0  # After 40s, cross spread to flatten

# Endgame
ENTRY_CUTOFF_SECS = 180
FLATTEN_DEADLINE_SECS = 120

# STALE ORDER REFRESH (V8)
STALE_DRIFT_CENTS = 5       # Cancel if order is 5c+ from current mid
STALE_TTL_SECS = 20         # Cancel if order sits for 20s without fill
CHASE_BLOCK_10S_CENTS = 8   # Don't repost if market moved 8c+ in last 10s
REPOST_COOLDOWN_SECS = 5    # 5s cooldown after stale cancel (prevents pyramiding)


@dataclass
class ExitState:
    """Tracks exit order state per token"""
    token_id: str
    shares: float
    entry_price: float
    entry_time: float
    
    # Current exit order (only ONE allowed)
    exit_order_id: Optional[str] = None
    exit_price: float = 0.0
    exit_posted_at: float = 0.0
    
    # State
    phase: str = "TP"  # "TP", "SCRATCH", "FLATTEN"
    
    @property
    def age_seconds(self) -> float:
        return time.time() - self.entry_time
    
    @property
    def time_since_exit_post(self) -> float:
        if self.exit_posted_at == 0:
            return 999
        return time.time() - self.exit_posted_at


@dataclass
class LoopMetrics:
    """Metrics for the main loop"""
    ticks: int = 0
    entries_posted: int = 0
    entries_blocked_inventory: int = 0  # NEW: blocked due to existing inventory
    entries_blocked_pending: int = 0    # NEW: blocked due to pending entry order
    entries_blocked_regime: int = 0     # NEW: blocked by regime filters
    entries_blocked_late: int = 0
    
    exits_posted: int = 0
    exits_repriced: int = 0
    exits_filled: int = 0
    exit_cancel_confirm_cycles: int = 0  # NEW: proper cancel-confirm-post cycles
    
    round_trips: int = 0
    realized_pnl: float = 0.0
    
    balance_errors: int = 0
    api_errors: int = 0


class SafeRunnerV5:
    """
    Safe market making runner with ALL P0 FIXES + OPENING MODE.
    
    KEY INVARIANTS:
    1. If inventory > 0, NO new entries (global)
    2. Only ONE exit order per token (cancel-confirm-post)
    3. Dust positions are held to settlement (no spam)
    
    OPENING MODE (first 60s):
    - Allow entries even if later regime filter fails
    - But enforce: vol_5s <= 10c, spread >= 1c
    - Only 1 entry order per side (YES bid + NO bid)
    - If either side fills: cancel other side immediately, go EXIT_ONLY
    """
    
    def __init__(self, config: Config, yes_token: str, no_token: str, market_end_time: int):
        self.config = config
        self.yes_token = yes_token
        self.no_token = no_token
        self.market_end_time = market_end_time
        
        self.live = config.mode == RunMode.LIVE
        
        # Components
        self.clob = ClobWrapper(config)
        self.position_manager = PositionManager(config)
        self.position_manager.set_market_tokens(yes_token, no_token)
        self.fill_tracker = FillTracker(config)
        
        # State
        self.state = BotState.STARTUP
        self.metrics = LoopMetrics()
        
        # OPENING MODE: Calculate window start time
        self.window_start_time = market_end_time - 900  # 15 min = 900s
        self.opening_end_time = self.window_start_time + OPENING_MODE_SECS
        
        # OPENING MODE: Track entry orders per side (max 1 each)
        self.opening_yes_order_id: Optional[str] = None
        self.opening_no_order_id: Optional[str] = None
        self.opening_entries_posted = 0  # Track total entries in opening
        
        # P0 FIX 1: Inventory tracking (REST is source of truth)
        self.rest_yes_shares = 0.0
        self.rest_no_shares = 0.0
        self.has_inventory = False
        
        # P0 FIX V9: Close debounce - don't close on single zero read
        self._yes_zero_count = 0  # Consecutive zero polls for YES
        self._no_zero_count = 0   # Consecutive zero polls for NO
        self._ZERO_DEBOUNCE = 3   # Require 3 consecutive zeros before closing
        self._inventory_latched_yes = False  # Once we have inventory, stay latched
        self._inventory_latched_no = False
        
        # P0 FIX 2: Exit state per token (only ONE exit order allowed)
        self.exit_states: Dict[str, ExitState] = {}
        
        # V8 FIX: Track last exit attempt time to prevent spam on BALANCE_ERROR
        self._last_exit_attempt: Dict[str, float] = {}
        self._exit_retry_cooldown_s = 3.0  # Wait 3s between exit attempts after failure
        
        # P0 FIX: Pending entry tracking (don't place more while order exists)
        self.pending_entry_token: Optional[str] = None
        self.pending_entry_order_id: Optional[str] = None
        self.pending_entry_price: float = 0.0        # V8: Track entry price for stale detection
        self.pending_entry_posted_at: float = 0.0    # V8: Track when entry was posted
        
        # P0 FIX V6: Track ALL entry orders + cooldown to prevent pyramiding
        self.all_entry_order_ids: Set[str] = set()  # ALL posted entry orders
        self.last_entry_posted_at: float = 0.0       # Cooldown: no entries for N seconds after post
        self.ENTRY_COOLDOWN_SECS = 15.0             # LONGER cooldown: 15s to wait for fill/cancel
        
        # V8: Stale order management
        self.last_stale_cancel_at: float = 0.0      # Cooldown after stale cancel
        self.chase_blocked_until: float = 0.0        # Don't repost until this time (trend protection)
        
        # P1: Rolling mid for regime filter (5s = 50 ticks at 100ms)
        self.mid_history: deque = deque(maxlen=50)
        
        # Timing - OPTION A: Faster polling for HFT-like behavior
        self.loop_interval_ms = 100  # Was 250ms, now 100ms (4x faster)
        self.reconcile_interval_s = 2.0  # REST reconcile every 2s
        self.reconcile_fast_s = 0.5  # When holding inventory: 500ms
        self.last_reconcile = 0.0
        
        # Shutdown
        self.running = False
        self.shutdown_event = threading.Event()
        
        # Lock file
        self.lock_file = Path("mm_bot.lock")
        self._acquired_lock = False
        
        # Output
        self.log_file = None
    
    @property
    def seconds_since_window_start(self) -> float:
        return time.time() - self.window_start_time
    
    @property
    def in_opening_mode(self) -> bool:
        """True if within first OPENING_MODE_SECS of window"""
        return time.time() < self.opening_end_time
    
    def _acquire_lock(self) -> bool:
        if self.lock_file.exists():
            try:
                with open(self.lock_file) as f:
                    pid = int(f.read().strip())
                try:
                    import psutil
                    if psutil.pid_exists(pid):
                        print(f"[SAFETY] Another runner is active (PID {pid})", flush=True)
                        return False
                except ImportError:
                    pass
            except:
                pass
        
        with open(self.lock_file, 'w') as f:
            f.write(str(os.getpid()))
        self._acquired_lock = True
        return True
    
    def _release_lock(self):
        if self._acquired_lock and self.lock_file.exists():
            self.lock_file.unlink()
            self._acquired_lock = False
    
    def _window_start_cleanup(self):
        """
        V9 FIX: Cancel ALL open orders at window start.
        This prevents leftover orders from prior windows filling unexpectedly.
        Also reset debounce counters for clean state.
        """
        # Reset debounce counters for clean state
        self._yes_zero_count = 0
        self._no_zero_count = 0
        
        print("[CLEANUP] Window start - cancelling all open orders...", flush=True)
        try:
            open_orders = self.clob.get_open_orders()
            if not open_orders:
                print("[CLEANUP] No open orders found", flush=True)
                return
            
            cancelled = 0
            for order in open_orders:
                try:
                    self.clob.cancel_order(order.order_id)
                    cancelled += 1
                except Exception as e:
                    print(f"[CLEANUP] Failed to cancel {order.order_id[:8]}...: {e}", flush=True)
            
            if cancelled > 0:
                print(f"[CLEANUP] Cancelled {cancelled} orders", flush=True)
                time.sleep(0.5)  # Wait for cancels to process
            
            # Confirm all cancelled
            remaining = self.clob.get_open_orders()
            if remaining:
                print(f"[CLEANUP] WARNING: {len(remaining)} orders still open after cleanup", flush=True)
                # Try cancel_all as fallback
                try:
                    self.clob.cancel_all()
                    time.sleep(0.5)
                except:
                    pass
            else:
                print("[CLEANUP] Confirmed: 0 open orders", flush=True)
                
        except Exception as e:
            print(f"[CLEANUP] Error during cleanup: {e}", flush=True)
            # Fallback: try cancel_all
            try:
                self.clob.cancel_all()
                print("[CLEANUP] Used cancel_all fallback", flush=True)
                time.sleep(0.5)
            except:
                pass
    
    @property
    def seconds_to_settlement(self) -> int:
        return max(0, self.market_end_time - int(time.time()))
    
    def _get_book(self, token_id: str) -> dict:
        try:
            book = self.clob.get_order_book(token_id)
            if book and book.best_bid > 0.01 and book.best_ask < 0.99:
                return {
                    "best_bid": book.best_bid,
                    "best_ask": book.best_ask,
                    "has_liquidity": book.has_liquidity
                }
        except Exception as e:
            self.metrics.api_errors += 1
        return {"best_bid": 0, "best_ask": 0, "has_liquidity": False}
    
    # ========================================================================
    # P0 FIX 1: INVENTORY GATING
    # ========================================================================
    
    def _reconcile_positions(self) -> bool:
        """
        Fetch REST positions. Returns True if changed.
        
        V9 FIX: Debounce close - don't close on single zero read.
        Require ZERO_DEBOUNCE consecutive zeros AND no open exit orders.
        """
        try:
            import requests
            r = requests.get(
                "https://data-api.polymarket.com/positions",
                params={"user": self.config.api.proxy_address},
                timeout=10
            )
            
            if r.status_code != 200:
                return False
            
            positions = r.json()
            
            old_yes = self.rest_yes_shares
            old_no = self.rest_no_shares
            
            # Read current REST values
            rest_yes = 0.0
            rest_no = 0.0
            avg_price_yes = 0.0
            avg_price_no = 0.0
            
            for p in positions:
                token = p.get("asset", "")
                shares = float(p.get("size", 0))
                avg_price = float(p.get("avgPrice", 0) or p.get("avg_price", 0) or p.get("averagePrice", 0))
                
                if token == self.yes_token:
                    rest_yes = shares
                    avg_price_yes = avg_price
                elif token == self.no_token:
                    rest_no = shares
                    avg_price_no = avg_price
            
            # V10 FIX: Reconcile is SANITY ONLY, not source of position changes.
            # Position open/close comes from confirmed fills in _poll_and_process_fills.
            # Here we just update rest_shares for sanity checking.
            
            # YES: Track REST value with debounce
            if rest_yes > 0.01:
                self._yes_zero_count = 0  # Reset on any non-zero
                self.rest_yes_shares = rest_yes
                # Note: Do NOT create exit state from reconcile - wait for confirmed fill
            else:
                # Got zero - only log if we think we have inventory
                if self._inventory_latched_yes:
                    self._yes_zero_count += 1
                    if self._yes_zero_count <= self._ZERO_DEBOUNCE:
                        # Still in debounce window - log but keep old value
                        print(f"[DEBOUNCE] YES zero read (count={self._yes_zero_count}/{self._ZERO_DEBOUNCE})", flush=True)
                    elif self._yes_zero_count == self._ZERO_DEBOUNCE + 1:
                        # Threshold reached - log sanity warning but do NOT close position
                        # Position close ONLY happens from confirmed exit fill
                        print(f"[SANITY] YES REST=0 after debounce, waiting for exit fill confirmation", flush=True)
                    # Don't keep incrementing - cap at threshold + 1
                    self._yes_zero_count = min(self._yes_zero_count, self._ZERO_DEBOUNCE + 1)
            
            # NO: Track REST value with debounce
            if rest_no > 0.01:
                self._no_zero_count = 0  # Reset on any non-zero
                self.rest_no_shares = rest_no
            else:
                if self._inventory_latched_no:
                    self._no_zero_count += 1
                    if self._no_zero_count <= self._ZERO_DEBOUNCE:
                        print(f"[DEBOUNCE] NO zero read (count={self._no_zero_count}/{self._ZERO_DEBOUNCE})", flush=True)
                    elif self._no_zero_count == self._ZERO_DEBOUNCE + 1:
                        print(f"[SANITY] NO REST=0 after debounce, waiting for exit fill confirmation", flush=True)
                    self._no_zero_count = min(self._no_zero_count, self._ZERO_DEBOUNCE + 1)
            
            # V10 FIX: has_inventory is based on exit_states (from confirmed fills)
            # Not from REST positions (which can flap)
            self.has_inventory = bool(self.exit_states)
            
            # Log REST values for sanity (but don't act on changes)
            if abs(old_yes - rest_yes) > 0.01 or abs(old_no - rest_no) > 0.01:
                print(f"[RECONCILE-SANITY] REST: YES={rest_yes:.2f} NO={rest_no:.2f}", flush=True)
            
            return False  # Reconcile never triggers state changes now
        
        except Exception as e:
            print(f"[RECONCILE] Error: {e}", flush=True)
            return False
    
    def _create_exit_state(self, token_id: str, shares: float, avg_price: float, from_fill: bool = False):
        """
        Create exit state for a new position.
        
        V10 FIX: Only create if from_fill=True (confirmed fill from trades API).
        Reconcile-based synthetic entries are REMOVED.
        """
        self.exit_states[token_id] = ExitState(
            token_id=token_id,
            shares=shares,
            entry_price=avg_price,
            entry_time=time.time()
        )
        
        source = "FILL" if from_fill else "RECONCILE"
        print(f"[POSITION] New: {token_id[:20]}... {shares:.2f} @ {avg_price:.4f} (from {source})", flush=True)
    
    def _on_position_closed(self, token_id: str, old_shares: float):
        """Handle position fully closed"""
        if token_id in self.exit_states:
            state = self.exit_states[token_id]
            hold_time = state.age_seconds
            print(f"[POSITION] Closed: {token_id[:20]}... held {hold_time:.1f}s", flush=True)
            del self.exit_states[token_id]
        
        self.metrics.exits_filled += 1
        self.metrics.round_trips += 1
    
    # ========================================================================
    # P0 FIX V6: CANCEL ALL ENTRY ORDERS + COOLDOWN
    # ========================================================================
    
    def _cancel_all_entry_orders(self) -> int:
        """
        Cancel ALL known entry orders.
        Returns number of orders cancelled.
        
        This prevents pyramiding by ensuring only 1 entry at a time.
        """
        cancelled = 0
        
        # Cancel all tracked entry orders
        orders_to_cancel = list(self.all_entry_order_ids)
        
        for order_id in orders_to_cancel:
            try:
                self.clob.cancel_order(order_id)
                cancelled += 1
            except:
                pass
        
        # Also cancel specific tracking
        if self.pending_entry_order_id and self.pending_entry_order_id not in orders_to_cancel:
            try:
                self.clob.cancel_order(self.pending_entry_order_id)
                cancelled += 1
            except:
                pass
        
        if self.opening_yes_order_id and self.opening_yes_order_id not in orders_to_cancel:
            try:
                self.clob.cancel_order(self.opening_yes_order_id)
                cancelled += 1
            except:
                pass
        
        if self.opening_no_order_id and self.opening_no_order_id not in orders_to_cancel:
            try:
                self.clob.cancel_order(self.opening_no_order_id)
                cancelled += 1
            except:
                pass
        
        # Clear tracking
        self.all_entry_order_ids.clear()
        self.pending_entry_order_id = None
        self.pending_entry_token = None
        self.opening_yes_order_id = None
        self.opening_no_order_id = None
        
        if cancelled > 0:
            print(f"[SAFETY] Cancelled {cancelled} entry orders", flush=True)
        
        return cancelled
    
    def _check_entry_cooldown(self) -> bool:
        """
        Check if we're in the entry cooldown period.
        Returns True if we CAN place an entry (cooldown passed).
        
        V8: Use shorter cooldown after stale cancel to allow quick repost.
        """
        if self.last_entry_posted_at == 0:
            return True
        
        now = time.time()
        elapsed = now - self.last_entry_posted_at
        
        # V8: If we just did a stale cancel, use shorter cooldown
        if self.last_stale_cancel_at > 0 and (now - self.last_stale_cancel_at) < 5.0:
            # Recent stale cancel - use repost cooldown
            if elapsed < REPOST_COOLDOWN_SECS:
                return False
            return True
        
        # Normal cooldown
        if elapsed < self.ENTRY_COOLDOWN_SECS:
            if self.metrics.ticks % 40 == 0:
                remaining = self.ENTRY_COOLDOWN_SECS - elapsed
                print(f"[COOLDOWN] {remaining:.0f}s remaining", flush=True)
            return False
        return True
    
    # ========================================================================
    # V8: STALE ORDER MANAGEMENT
    # ========================================================================
    
    def _get_10s_directional_move(self) -> float:
        """
        Calculate directional price movement over last 10s (~25 ticks).
        Returns signed move in cents (positive = up, negative = down).
        """
        if len(self.mid_history) < 25:
            return 0.0
        
        mids = list(self.mid_history)
        old_mid = mids[-25] if len(mids) >= 25 else mids[0]
        new_mid = mids[-1]
        return (new_mid - old_mid) * 100
    
    def _check_stale_entry(self, yes_mid: float, no_mid: float) -> Optional[str]:
        """
        Check if pending entry order is stale and should be cancelled.
        Returns reason string if stale, None if still valid.
        """
        if not self.pending_entry_order_id:
            return None
        
        now = time.time()
        
        # Trigger 1: Endgame or extreme odds
        if self.seconds_to_settlement < ENTRY_CUTOFF_SECS:
            return "endgame"
        
        if yes_mid < 0.10 or yes_mid > 0.90:
            return "extreme"
        
        # Trigger 2: Price drift too far
        # FIX: Use the mid of the token we're trading, not always yes_mid
        if self.pending_entry_token == self.yes_token:
            current_mid = yes_mid
        else:
            current_mid = no_mid
        
        drift_cents = abs(current_mid - self.pending_entry_price) * 100
        if drift_cents >= STALE_DRIFT_CENTS:
            return f"drift({drift_cents:.1f}c)"
        
        # Trigger 3: TTL expired
        order_age = now - self.pending_entry_posted_at
        if order_age >= STALE_TTL_SECS:
            return f"ttl({order_age:.0f}s)"
        
        return None
    
    def _is_chase_blocked(self) -> bool:
        """
        Check if we're in a trending market (should not chase).
        Returns True if reposting should be blocked.
        """
        # Check if still in cooldown from previous chase block
        if time.time() < self.chase_blocked_until:
            return True
        
        # Check 10s directional move
        move_cents = abs(self._get_10s_directional_move())
        if move_cents >= CHASE_BLOCK_10S_CENTS:
            # Block for 5 seconds
            self.chase_blocked_until = time.time() + 5.0
            print(f"[CHASE_BLOCK] Market moved {move_cents:.1f}c in 10s, blocking repost", flush=True)
            return True
        
        return False
    
    # ========================================================================
    # V10 FIX: FILL-BASED POSITION TRACKING (SOURCE OF TRUTH)
    # ========================================================================
    
    def _poll_and_process_fills(self):
        """
        V10 FIX: Poll trades API for actual fills.
        This is the SOURCE OF TRUTH for position changes.
        Reconcile is sanity-only.
        """
        market_tokens = {self.yes_token, self.no_token}
        new_fills = self.fill_tracker.poll_fills(market_tokens)
        
        for fill in new_fills:
            self._process_confirmed_fill(fill)
    
    def _process_confirmed_fill(self, fill):
        """
        Process a confirmed fill from trades API.
        
        BUY fill = entry (we bought shares)
        SELL fill = exit (we sold shares)
        """
        token_id = fill.token_id
        side = fill.side.upper()
        size = fill.size
        price = fill.price
        is_maker = fill.is_maker
        trade_id = fill.trade_id[:12] if fill.trade_id else "unknown"
        
        label = "YES" if token_id == self.yes_token else "NO"
        maker_str = "MAKER" if is_maker else "TAKER"
        
        if side == "BUY":
            # Entry fill
            print(f"[FILL] ENTRY {label}: {size:.2f} @ {price:.4f} ({maker_str}) trade_id={trade_id}", flush=True)
            
            # Create/update exit state from confirmed fill
            if token_id not in self.exit_states:
                self._create_exit_state(token_id, size, price, from_fill=True)
            else:
                # Add to existing position
                state = self.exit_states[token_id]
                old_shares = state.shares
                new_shares = old_shares + size
                # Weighted avg price
                state.entry_price = (state.entry_price * old_shares + price * size) / new_shares
                state.shares = new_shares
                print(f"[POSITION] Updated: {label} now {new_shares:.2f} @ {state.entry_price:.4f}", flush=True)
            
            # Cancel other side's entries (inventory rule)
            other_token = self.no_token if token_id == self.yes_token else self.yes_token
            self._cancel_other_side_on_fill(token_id)
            
            # Clear pending entry tracking
            if self.pending_entry_token == token_id:
                self.pending_entry_order_id = None
                self.pending_entry_token = None
            
        elif side == "SELL":
            # Exit fill
            state = self.exit_states.get(token_id)
            if state:
                entry_price = state.entry_price
                pnl = (price - entry_price) * size
                pnl_str = f"+${pnl:.2f}" if pnl >= 0 else f"-${abs(pnl):.2f}"
                
                print(f"[FILL] EXIT {label}: {size:.2f} @ {price:.4f} ({maker_str}) trade_id={trade_id}", flush=True)
                print(f"[ROUND-TRIP] Entry={entry_price:.4f} Exit={price:.4f} Size={size:.2f} PnL={pnl_str} (from fills, not cash delta)", flush=True)
                
                # Update position
                state.shares -= size
                if state.shares < 0.01:
                    # Position fully closed
                    self._on_position_closed_from_fill(token_id, entry_price, price, size)
            else:
                print(f"[FILL] EXIT {label}: {size:.2f} @ {price:.4f} ({maker_str}) trade_id={trade_id} - NO MATCHING ENTRY", flush=True)
    
    def _on_position_closed_from_fill(self, token_id: str, entry_price: float, exit_price: float, size: float):
        """
        Handle position close from confirmed exit fill.
        This is the CORRECT way to close a position.
        """
        label = "YES" if token_id == self.yes_token else "NO"
        pnl = (exit_price - entry_price) * size
        pnl_str = f"+${pnl:.2f}" if pnl >= 0 else f"-${abs(pnl):.2f}"
        
        state = self.exit_states.get(token_id)
        hold_time = time.time() - state.entry_time if state else 0
        
        print(f"[POSITION] Closed {label}: held {hold_time:.1f}s, realized PnL={pnl_str}", flush=True)
        
        # Clean up state
        if token_id in self.exit_states:
            del self.exit_states[token_id]
        
        # Reset inventory latch
        if token_id == self.yes_token:
            self._inventory_latched_yes = False
            self._yes_zero_count = 0
        else:
            self._inventory_latched_no = False
            self._no_zero_count = 0
        
        # Update metrics
        if pnl >= 0:
            self.metrics.wins += 1
        else:
            self.metrics.losses += 1
    
    def _manage_stale_entry(self, yes_mid: float, no_mid: float):
        """
        Check and handle stale entry orders.
        Cancel if stale, optionally repost if conditions are safe.
        """
        stale_reason = self._check_stale_entry(yes_mid, no_mid)
        if not stale_reason:
            return  # Order still valid
        
        # Cancel the stale order
        order_id = self.pending_entry_order_id
        try:
            self.clob.cancel_order(order_id)
            print(f"[ENTRY_STALE_CANCEL] reason={stale_reason}", flush=True)
        except:
            pass
        
        # Clear tracking
        if order_id in self.all_entry_order_ids:
            self.all_entry_order_ids.discard(order_id)
        self.pending_entry_order_id = None
        self.pending_entry_token = None
        self.pending_entry_price = 0
        self.pending_entry_posted_at = 0
        self.last_stale_cancel_at = time.time()
        
        # Decide whether to repost
        if stale_reason in ("endgame", "extreme"):
            # Don't repost in these cases
            return
        
        # Check repost cooldown
        if time.time() - self.last_stale_cancel_at < REPOST_COOLDOWN_SECS:
            return
        
        # Check chase block
        if self._is_chase_blocked():
            return
        
        # Check regime filter
        can_enter, reason = self._check_regime(yes_mid, no_mid)
        if not can_enter:
            print(f"[ENTRY_REPOST_BLOCKED] regime={reason}", flush=True)
            return
        
        # Regime valid, repost allowed
        # The normal entry logic will handle posting on next tick
        print(f"[ENTRY_REPOST_ALLOWED] Will repost on next tick", flush=True)
    
    # ========================================================================
    # P0 FIX 2: EXIT REPLACE = CANCEL-CONFIRM-POST
    # ========================================================================
    
    def _cancel_confirm_exit(self, token_id: str) -> bool:
        """
        Cancel existing exit order and CONFIRM it's gone before returning.
        Returns True if safe to post new exit.
        """
        state = self.exit_states.get(token_id)
        if not state or not state.exit_order_id:
            return True  # No exit order exists, safe to post
        
        try:
            # Cancel
            self.clob.cancel_order(state.exit_order_id)
            
            # Confirm by polling open orders
            for _ in range(3):
                time.sleep(0.3)
                open_orders = self.clob.get_open_orders()
                
                still_exists = any(
                    o.order_id == state.exit_order_id 
                    for o in open_orders
                )
                
                if not still_exists:
                    state.exit_order_id = None
                    state.exit_price = 0
                    self.metrics.exit_cancel_confirm_cycles += 1
                    return True
            
            print(f"[EXIT] Cancel-confirm failed for {token_id[:20]}...", flush=True)
            return False
        
        except Exception as e:
            print(f"[EXIT] Cancel error: {e}", flush=True)
            return False
    
    def _post_exit_order(self, token_id: str, price: float, post_only: bool = True) -> bool:
        """
        Post exit order with safety checks.
        
        V6 FIX: Always cancel any existing exit first, then refresh shares.
        V8 FIX: Cancel ALL orders for this token to prevent BALANCE_ERROR from orphaned orders.
        V9 FIX: Add invariant logging + assertions for correct side/token.
        V10 FIX: Use shares from exit_state (confirmed fills) not REST.
        """
        # Get exit state first - this is source of truth from fills
        state = self.exit_states.get(token_id)
        if not state:
            return False
        
        # V10: Use shares from exit_state (confirmed fills)
        shares = state.shares
        
        # Determine token label for logging
        is_yes_token = (token_id == self.yes_token)
        token_label = "YES" if is_yes_token else "NO"
        
        # DUST CHECK FIRST (before any operations)
        if shares < MIN_SHARES:
            if shares > 0:
                dust_key = f"dust_{token_id}"
                if not hasattr(self, '_dust_logged') or dust_key not in self._dust_logged:
                    if not hasattr(self, '_dust_logged'):
                        self._dust_logged = set()
                    self._dust_logged.add(dust_key)
                    print(f"[DUST] {token_label} has {shares:.2f} < {MIN_SHARES} min, holding to settlement", flush=True)
            return False
        
        # V10: Log exit intent with full context BEFORE placing
        print(f"[EXIT-INVARIANT] token={token_label} side=SELL size={shares:.2f} price={price:.4f}", flush=True)
        
        # V8 FIX: Cancel ALL open SELL orders for this token to prevent BALANCE_ERROR
        try:
            open_orders = self.clob.get_open_orders()
            cancelled_count = 0
            for order in open_orders:
                order_token = getattr(order, 'asset', None) or getattr(order, 'token_id', None)
                order_side = getattr(order, 'side', None)
                if order_token == token_id and order_side == "SELL":
                    try:
                        self.clob.cancel_order(order.order_id)
                        cancelled_count += 1
                    except:
                        pass
            if cancelled_count > 0:
                print(f"[EXIT] Cancelled {cancelled_count} existing SELL orders", flush=True)
                time.sleep(0.5)  # Wait for cancels to process
        except Exception as e:
            print(f"[EXIT] Error checking orders: {e}", flush=True)
        
        # Clear tracked exit order since we cancelled
        state.exit_order_id = None
        state.exit_price = 0
        
        # V10: Use shares from exit_state (confirmed fills), not REST
        sell_size = int(shares / SHARE_STEP) * SHARE_STEP
        sell_size = min(sell_size, shares)
        
        # Invariant: never sell more than we have
        if sell_size > shares:
            print(f"[EXIT] INVARIANT FAIL: sell_size={sell_size:.2f} > shares={shares:.2f}", flush=True)
            return False
        
        try:
            result = self.clob.post_order(
                token_id=token_id,
                side="SELL",
                price=price,
                size=sell_size,
                post_only=post_only
            )
            
            if result.success and result.order_id:
                state.exit_order_id = result.order_id
                state.exit_price = price
                state.exit_posted_at = time.time()
                self.metrics.exits_posted += 1
                # Clear cooldown on success
                self._last_exit_attempt.pop(token_id, None)
                
                phase_str = state.phase
                print(f"[EXIT] Posted ({phase_str}): {sell_size:.2f} @ {price:.4f}", flush=True)
                return True
            else:
                if result.error:
                    error_str = str(result.error).lower()
                    if "balance" in error_str or "allowance" in error_str:
                        self.metrics.balance_errors += 1
                        # V8 FIX: Set cooldown to prevent spam
                        self._last_exit_attempt[token_id] = time.time()
                        print(f"[EXIT] BALANCE_ERROR: size={sell_size:.2f} price={price:.4f} (retry in {self._exit_retry_cooldown_s}s)", flush=True)
                    else:
                        print(f"[EXIT] Failed: {result.error}", flush=True)
        
        except Exception as e:
            print(f"[EXIT] Error: {e}", flush=True)
            self.metrics.api_errors += 1
        
        return False
    
    def _post_exit_order_raw(self, token_id: str, price: float, size: float, post_only: bool, state: ExitState) -> bool:
        """Raw exit order post (no cancel/refresh, used for retries with smaller size)"""
        try:
            result = self.clob.post_order(
                token_id=token_id,
                side="SELL",
                price=price,
                size=size,
                post_only=post_only
            )
            
            if result.success and result.order_id:
                state.exit_order_id = result.order_id
                state.exit_price = price
                state.exit_posted_at = time.time()
                self.metrics.exits_posted += 1
                print(f"[EXIT] Posted ({state.phase}): {size:.2f} @ {price:.4f}", flush=True)
                return True
        except:
            pass
        return False
    
    # ========================================================================
    # P1 FIX: TIME-BASED EXIT LADDER
    # ========================================================================
    
    def _manage_exit(self, token_id: str, book: dict):
        """
        Manage exit for a position using time-based ladder.
        
        Phase 1 (TP): Post exit at entry + TP_CENTS (maker)
        Phase 2 (SCRATCH): After 20s, reprice to entry (scratch)
        Phase 3 (FLATTEN): After 40s, cross spread to flatten (taker if enabled)
        
        V10 FIX: Use shares from exit_state (confirmed fills) not REST.
        """
        state = self.exit_states.get(token_id)
        if not state:
            return
        
        # V10: Use shares from exit_state (source of truth from fills)
        shares = state.shares
        if shares < 0.01:
            # Position closed (from fill)
            return
        
        # V8 FIX: Cooldown after failed exit attempt to prevent BALANCE_ERROR spam
        last_attempt = self._last_exit_attempt.get(token_id, 0)
        if time.time() - last_attempt < self._exit_retry_cooldown_s:
            return  # Still in cooldown, don't spam
        
        # Early dust check - don't try to manage exit for dust positions
        if shares < MIN_SHARES:
            return
        
        best_bid = book["best_bid"]
        best_ask = book["best_ask"]
        
        if best_bid < 0.02:
            return  # No valid book
        
        # Determine phase based on time
        age = state.age_seconds
        
        if age < EXIT_SCRATCH_SECS:
            state.phase = "TP"
            target_price = min(0.99, state.entry_price + EXIT_TP_CENTS / 100.0)
        elif age < EXIT_FLATTEN_SECS:
            state.phase = "SCRATCH"
            target_price = state.entry_price  # Scratch = break-even
        else:
            state.phase = "FLATTEN"
            # Cross the spread
            emergency_enabled = self.config.risk.emergency_taker_exit
            if emergency_enabled:
                target_price = best_bid  # Take liquidity
            else:
                target_price = best_ask - 0.01  # Best maker price
        
        target_price = max(0.01, min(0.99, target_price))
        
        # Check if we need to post or reprice
        if not state.exit_order_id:
            # No exit order - post one
            self._post_exit_order(token_id, target_price, post_only=(state.phase != "FLATTEN"))
        
        elif abs(state.exit_price - target_price) >= 0.01:
            # Need to reprice
            if state.time_since_exit_post >= 2.0:  # Throttle repricing
                if self._cancel_confirm_exit(token_id):
                    self._post_exit_order(token_id, target_price, post_only=(state.phase != "FLATTEN"))
                    self.metrics.exits_repriced += 1
    
    # ========================================================================
    # P1 FIX: REGIME FILTERS
    # ========================================================================
    
    def _check_regime(self, yes_mid: float, no_mid: float) -> Tuple[bool, str]:
        """
        Check if market conditions allow entry.
        Returns (can_enter, reason_if_blocked)
        """
        # Mid range filter
        if yes_mid < ENTRY_MID_MIN or yes_mid > ENTRY_MID_MAX:
            return False, f"REGIME: yes_mid={yes_mid:.2f} outside [{ENTRY_MID_MIN}, {ENTRY_MID_MAX}]"
        
        # Volatility filter (10s window)
        if len(self.mid_history) >= 20:  # ~5s of data
            mids = list(self.mid_history)
            mid_min = min(mids)
            mid_max = max(mids)
            vol_cents = (mid_max - mid_min) * 100
            
            if vol_cents > VOL_10S_CENTS:
                return False, f"REGIME: vol={vol_cents:.1f}c > {VOL_10S_CENTS}c"
        
        return True, ""
    
    # ========================================================================
    # OPENING MODE ENTRY LOGIC (first 60s of window)
    # ========================================================================
    
    def _try_opening_entry(self, yes_book: dict, no_book: dict) -> int:
        """
        OPENING MODE: Place up to 1 entry per side (YES bid + NO bid).
        Returns count of entries posted this tick.
        
        Rules:
        - Max 1 entry per side total
        - vol_5s <= OPEN_VOL_MAX
        - spread >= OPEN_MIN_SPREAD
        - If either fills: cancel other side immediately
        - V6: Cooldown between entries
        """
        
        # P0 FIX 1: INVENTORY GATE (still applies!)
        if self.has_inventory:
            self.metrics.entries_blocked_inventory += 1
            return 0
        
        # V6: Cooldown check
        if not self._check_entry_cooldown():
            return 0
        
        entries_posted = 0
        
        # Calculate 5s volatility
        if len(self.mid_history) >= 10:
            mids = list(self.mid_history)[-10:]  # Last ~2.5s
            vol_cents = (max(mids) - min(mids)) * 100
            if vol_cents > OPEN_VOL_MAX_CENTS:
                if self.metrics.ticks % 8 == 0:
                    print(f"[OPENING] Vol too high: {vol_cents:.1f}c > {OPEN_VOL_MAX_CENTS}c", flush=True)
                return 0
        
        # Try YES side (if no order exists yet)
        if self.opening_yes_order_id is None:
            yes_spread = (yes_book["best_ask"] - yes_book["best_bid"]) * 100
            if yes_spread >= OPEN_MIN_SPREAD_CENTS:
                order_id = self._place_opening_entry(self.yes_token, yes_book, "YES")
                if order_id:
                    self.opening_yes_order_id = order_id
                    entries_posted += 1
        
        # Try NO side (if no order exists yet)
        if self.opening_no_order_id is None:
            no_spread = (no_book["best_ask"] - no_book["best_bid"]) * 100
            if no_spread >= OPEN_MIN_SPREAD_CENTS:
                order_id = self._place_opening_entry(self.no_token, no_book, "NO")
                if order_id:
                    self.opening_no_order_id = order_id
                    entries_posted += 1
        
        return entries_posted
    
    def _place_opening_entry(self, token_id: str, book: dict, label: str) -> Optional[str]:
        """Place a single opening mode entry"""
        desired_bid = book["best_bid"]
        quote_size = MIN_QUOTE_SHARES  # Use slightly more than min to avoid dust from partial fills
        
        cost = quote_size * desired_bid
        if cost > self.config.risk.max_usdc_locked:
            return None
        
        if not self.live:
            print(f"[OPENING] Would place {label} BID {quote_size} @ {desired_bid:.4f}", flush=True)
            return None
        
        try:
            result = self.clob.post_order(
                token_id=token_id,
                side="BUY",
                price=desired_bid,
                size=quote_size,
                post_only=True
            )
            
            if result.success and result.order_id:
                # V6: Track order and set cooldown
                self.all_entry_order_ids.add(result.order_id)
                self.pending_entry_order_id = result.order_id    # V8: Track for stale detection
                self.pending_entry_token = token_id              # V8
                self.pending_entry_price = desired_bid           # V8
                self.pending_entry_posted_at = time.time()       # V8
                self.last_entry_posted_at = time.time()          # START COOLDOWN
                self.metrics.entries_posted += 1
                self.opening_entries_posted += 1
                print(f"[OPENING] Posted {label} BID {quote_size} @ {desired_bid:.4f}", flush=True)
                return result.order_id
            else:
                if result.error:
                    error_str = str(result.error).lower()
                    if "balance" in error_str or "allowance" in error_str:
                        self.metrics.balance_errors += 1
        
        except Exception as e:
            self.metrics.api_errors += 1
        
        return None
    
    def _cancel_other_side_on_fill(self, filled_token_id: str):
        """When one side fills, cancel the other side's entry immediately"""
        if filled_token_id == self.yes_token:
            # Cancel NO entry
            if self.opening_no_order_id:
                try:
                    self.clob.cancel_order(self.opening_no_order_id)
                    print(f"[OPENING] Cancelled NO entry (YES filled)", flush=True)
                except:
                    pass
                self.opening_no_order_id = None
        else:
            # Cancel YES entry
            if self.opening_yes_order_id:
                try:
                    self.clob.cancel_order(self.opening_yes_order_id)
                    print(f"[OPENING] Cancelled YES entry (NO filled)", flush=True)
                except:
                    pass
                self.opening_yes_order_id = None
    
    # ========================================================================
    # NORMAL MODE ENTRY LOGIC (after opening)
    # ========================================================================
    
    def _try_entry(self, yes_book: dict, no_book: dict) -> bool:
        """
        NORMAL MODE: Try to place an entry order.
        Returns True if entry was posted.
        
        P0 GUARDS (V6):
        1. No entry if ANY inventory exists
        2. COOLDOWN: No entry within 2s of last entry post
        3. Cancel ALL existing entry orders before posting new
        4. Regime filters must pass
        """
        
        # P0 FIX 1: INVENTORY GATE
        if self.has_inventory:
            self.metrics.entries_blocked_inventory += 1
            return False
        
        # P0 FIX V6: COOLDOWN CHECK (prevents rapid-fire posting)
        if not self._check_entry_cooldown():
            self.metrics.entries_blocked_pending += 1
            return False
        
        # P0 FIX V6: Check if we have ANY open entry orders
        # If yes, DON'T cancel - just wait for them to fill or be cancelled by inventory gate
        if self.all_entry_order_ids:
            try:
                open_orders = self.clob.get_open_orders()
                open_ids = {o.order_id for o in open_orders}
                still_open = self.all_entry_order_ids & open_ids
                
                if still_open:
                    # Entry order(s) still resting - wait for fill, don't post more
                    self.metrics.entries_blocked_pending += 1
                    return False
                else:
                    # All previous entries have been filled or cancelled
                    self.all_entry_order_ids.clear()
            except:
                self.metrics.entries_blocked_pending += 1
                return False
        
        # Endgame check
        if self.seconds_to_settlement < ENTRY_CUTOFF_SECS:
            self.metrics.entries_blocked_late += 1
            return False
        
        # Calculate mids
        yes_mid = (yes_book["best_bid"] + yes_book["best_ask"]) / 2
        no_mid = (no_book["best_bid"] + no_book["best_ask"]) / 2
        
        # P1: Regime filter
        can_enter, reason = self._check_regime(yes_mid, no_mid)
        if not can_enter:
            if self.metrics.ticks % 8 == 0:
                print(f"[NORMAL] Blocked: {reason}", flush=True)
            self.metrics.entries_blocked_regime += 1
            return False
        
        # Spread filter - now with logging
        yes_spread = yes_book["best_ask"] - yes_book["best_bid"]
        no_spread = no_book["best_ask"] - no_book["best_bid"]
        
        if yes_spread * 100 < MIN_SPREAD_CENTS and no_spread * 100 < MIN_SPREAD_CENTS:
            if self.metrics.ticks % 10 == 0:
                print(f"[NORMAL] Blocked: SPREAD too tight (YES={yes_spread*100:.1f}c, NO={no_spread*100:.1f}c < {MIN_SPREAD_CENTS}c)", flush=True)
            return False
        
        # Choose side with better spread
        if yes_spread >= no_spread:
            token_id = self.yes_token
            book = yes_book
            label = "YES"
        else:
            token_id = self.no_token
            book = no_book
            label = "NO"
        
        desired_bid = book["best_bid"]
        quote_size = max(MIN_QUOTE_SHARES, self.config.quoting.base_quote_size)
        
        cost = quote_size * desired_bid
        if cost > self.config.risk.max_usdc_locked:
            quote_size = int(self.config.risk.max_usdc_locked / desired_bid)
            if quote_size < MIN_QUOTE_SHARES:
                if self.metrics.ticks % 20 == 0:
                    print(f"[NORMAL] Blocked: SIZE too small after cap (need {MIN_QUOTE_SHARES}, got {quote_size} at ${desired_bid:.2f})", flush=True)
                return False
        
        if not self.live:
            print(f"[NORMAL] Would place {label} BID {quote_size} @ {desired_bid:.4f}", flush=True)
            return False
        
        try:
            result = self.clob.post_order(
                token_id=token_id,
                side="BUY",
                price=desired_bid,
                size=quote_size,
                post_only=True
            )
            
            if result.success and result.order_id:
                # V6: Track in all orders + set cooldown IMMEDIATELY
                self.all_entry_order_ids.add(result.order_id)
                self.pending_entry_order_id = result.order_id
                self.pending_entry_token = token_id
                self.pending_entry_price = desired_bid      # V8: Track price for stale detection
                self.pending_entry_posted_at = time.time()  # V8: Track when posted
                self.last_entry_posted_at = time.time()     # START COOLDOWN
                self.metrics.entries_posted += 1
                print(f"[NORMAL] Posted {label} BID {quote_size} @ {desired_bid:.4f}", flush=True)
                return True
            else:
                if result.error:
                    error_str = str(result.error).lower()
                    if "balance" in error_str or "allowance" in error_str:
                        self.metrics.balance_errors += 1
                        print(f"[NORMAL] BALANCE_ERROR: size={quote_size} cost=${cost:.2f}", flush=True)
        
        except Exception as e:
            self.metrics.api_errors += 1
        
        return False
    
    # ========================================================================
    # MAIN LOOP
    # ========================================================================
    
    def _run_tick(self):
        """Main loop tick (250ms)"""
        self.metrics.ticks += 1
        
        # Get books
        yes_book = self._get_book(self.yes_token)
        no_book = self._get_book(self.no_token)
        
        if not yes_book["has_liquidity"] or not no_book["has_liquidity"]:
            if self.metrics.ticks % 4 == 0:
                print("[TICK] No valid book", flush=True)
            return
        
        # Update mid history (for volatility calculation)
        yes_mid = (yes_book["best_bid"] + yes_book["best_ask"]) / 2
        no_mid = (no_book["best_bid"] + no_book["best_ask"]) / 2
        self.mid_history.append(yes_mid)
        
        # V10 FIX: Poll for actual fills from trades API (SOURCE OF TRUTH)
        self._poll_and_process_fills()
        
        # V8: Check for stale entry orders and manage them
        if self.pending_entry_order_id:
            self._manage_stale_entry(yes_mid, no_mid)
        
        # Reconcile - FAST when holding inventory (500ms), normal otherwise (2s)
        # V10: Reconcile is now SANITY ONLY, not source of position changes
        now = time.time()
        has_orders = (self.all_entry_order_ids or
                      self.pending_entry_order_id or 
                      self.opening_yes_order_id or 
                      self.opening_no_order_id or 
                      self.has_inventory)
        reconcile_interval = self.reconcile_fast_s if has_orders else self.reconcile_interval_s
        
        if now - self.last_reconcile >= reconcile_interval:
            self._reconcile_positions()
            self.last_reconcile = now
        
        # Determine state based on inventory and time
        if self.has_inventory:
            self.state = BotState.EXIT_ONLY
            
            # V6 FIX: Cancel ALL entry orders immediately when we have inventory
            if self.all_entry_order_ids or self.pending_entry_order_id or self.opening_yes_order_id or self.opening_no_order_id:
                self._cancel_all_entry_orders()
            
            # Manage exits
            if self.rest_yes_shares > 0.01:
                self._manage_exit(self.yes_token, yes_book)
            if self.rest_no_shares > 0.01:
                self._manage_exit(self.no_token, no_book)
            
        elif self.seconds_to_settlement < ENTRY_CUTOFF_SECS:
            # Past entry cutoff
            self.state = BotState.EXIT_ONLY
        
        elif self.in_opening_mode:
            # OPENING MODE: first 60s of window
            self.state = BotState.OPENING
            self._try_opening_entry(yes_book, no_book)
        
        else:
            # NORMAL MODE: after opening
            self.state = BotState.QUOTING
            self._try_entry(yes_book, no_book)
        
        # Log
        self._log_tick(yes_book, no_book)
    
    def _log_tick(self, yes_book: dict, no_book: dict):
        """Log current state"""
        if self.metrics.ticks % 4 != 0:
            return
        
        yes_mid = (yes_book["best_bid"] + yes_book["best_ask"]) / 2
        no_mid = (no_book["best_bid"] + no_book["best_ask"]) / 2
        
        secs_left = self.seconds_to_settlement
        time_str = f"{secs_left//60}:{secs_left%60:02d}"
        
        inv_str = f"YES:{self.rest_yes_shares:.1f} NO:{self.rest_no_shares:.1f}"
        
        state_str = self.state.value.upper()
        
        # Show opening mode status
        if self.state == BotState.OPENING:
            open_secs = int(self.opening_end_time - time.time())
            extra = f"OPEN:{open_secs}s YES-ord:{1 if self.opening_yes_order_id else 0} NO-ord:{1 if self.opening_no_order_id else 0}"
        elif self.pending_entry_order_id:
            extra = "PENDING"
        else:
            extra = ""
        
        print(f"[{state_str}] {time_str} | YES={yes_mid:.2f} NO={no_mid:.2f} | {inv_str} {extra}", flush=True)
        
        if self.log_file:
            entry = {
                "ts": datetime.now().isoformat(),
                "tick": self.metrics.ticks,
                "state": self.state.value,
                "secs_left": secs_left,
                "yes_shares": self.rest_yes_shares,
                "no_shares": self.rest_no_shares,
                "has_inventory": self.has_inventory,
                "in_opening": self.in_opening_mode,
                "opening_entries": self.opening_entries_posted
            }
            self.log_file.write(json.dumps(entry) + "\n")
            self.log_file.flush()
    
    def run(self, duration_seconds: float, output_dir: str = "mm_out"):
        """Run the bot for specified duration"""
        
        if self.live:
            if not os.environ.get("MM_EXIT_ENFORCED"):
                print("[SAFETY] LIVE mode requires MM_EXIT_ENFORCED=1", flush=True)
                return
        
        if not self._acquire_lock():
            return
        
        out_path = Path(output_dir)
        out_path.mkdir(exist_ok=True)
        self.log_file = open(out_path / "run.jsonl", "w")
        
        def on_signal(sig, frame):
            print("\n[SHUTDOWN] Signal received", flush=True)
            self.running = False
            self.shutdown_event.set()
        
        signal.signal(signal.SIGINT, on_signal)
        signal.signal(signal.SIGTERM, on_signal)
        
        try:
            print(f"[START] Mode={'LIVE' if self.live else 'DRYRUN'} Duration={duration_seconds}s", flush=True)
            print(f"[START] Market ends in {self.seconds_to_settlement}s", flush=True)
            print(f"[START] OPENING MODE: First {OPENING_MODE_SECS}s, vol<={OPEN_VOL_MAX_CENTS}c, spread>={OPEN_MIN_SPREAD_CENTS}c", flush=True)
            print(f"[START] NORMAL MODE: Regime [{ENTRY_MID_MIN}-{ENTRY_MID_MAX}], vol<={VOL_10S_CENTS}c", flush=True)
            print(f"[START] MIN_SHARES={MIN_SHARES}, MAX_USDC=${self.config.risk.max_usdc_locked:.2f}", flush=True)
            
            # Calculate opening mode timing
            secs_in_window = self.seconds_since_window_start
            if secs_in_window < OPENING_MODE_SECS:
                print(f"[START] Currently in OPENING MODE ({OPENING_MODE_SECS - secs_in_window:.0f}s remaining)", flush=True)
            else:
                print(f"[START] Past opening mode, NORMAL MODE active", flush=True)
            
            try:
                bal = self.clob.get_balance()
                print(f"[START] Session start cash: ${bal.get('usdc', 0):.2f}", flush=True)
            except:
                pass
            
            # V9 FIX: Window start cleanup - cancel ALL orders before quoting
            self._window_start_cleanup()
            
            # Initial reconcile
            self._reconcile_positions()
            
            if self.has_inventory:
                print("[STARTUP] Existing positions -> EXIT_ONLY mode", flush=True)
                self.state = BotState.EXIT_ONLY
            else:
                self.state = BotState.QUOTING
            
            self.running = True
            start_time = time.time()
            
            while self.running:
                tick_start = time.time()
                
                if tick_start - start_time >= duration_seconds:
                    print("[SHUTDOWN] Duration reached", flush=True)
                    break
                
                if self.seconds_to_settlement <= 0:
                    print("[SHUTDOWN] Market ended", flush=True)
                    break
                
                try:
                    self._run_tick()
                except Exception as e:
                    print(f"[ERROR] Tick error: {e}", flush=True)
                    self.metrics.api_errors += 1
                
                elapsed = time.time() - tick_start
                sleep_time = max(0, (self.loop_interval_ms / 1000.0) - elapsed)
                if sleep_time > 0:
                    self.shutdown_event.wait(sleep_time)
        
        finally:
            print("[SHUTDOWN] Cleaning up...", flush=True)
            
            # Cancel pending entry
            if self.pending_entry_order_id:
                try:
                    self.clob.cancel_order(self.pending_entry_order_id)
                except:
                    pass
            
            # Final reconcile
            self._reconcile_positions()
            
            self._write_report(out_path)
            
            if self.log_file:
                self.log_file.close()
            self._release_lock()
    
    def _write_report(self, out_path: Path):
        """Write final report"""
        report = {
            "timestamp": datetime.now().isoformat(),
            "mode": "LIVE" if self.live else "DRYRUN",
            "version": "V5",
            "p0_fixes": [
                "Inventory gating (no entries while holding)",
                "Cancel-confirm-post (never two SELLs)",
                "Dust policy (hold to settlement)"
            ],
            "p1_fixes": [
                f"Regime filter [{ENTRY_MID_MIN}-{ENTRY_MID_MAX}]",
                "Time-based exit ladder (TP/scratch/flatten)"
            ],
            "metrics": {
                "ticks": self.metrics.ticks,
                "entries_posted": self.metrics.entries_posted,
                "entries_blocked_inventory": self.metrics.entries_blocked_inventory,
                "entries_blocked_pending": self.metrics.entries_blocked_pending,
                "entries_blocked_regime": self.metrics.entries_blocked_regime,
                "entries_blocked_late": self.metrics.entries_blocked_late,
                "exits_posted": self.metrics.exits_posted,
                "exits_repriced": self.metrics.exits_repriced,
                "exit_cancel_confirm_cycles": self.metrics.exit_cancel_confirm_cycles,
                "round_trips": self.metrics.round_trips,
                "balance_errors": self.metrics.balance_errors,
                "api_errors": self.metrics.api_errors
            },
            "final_positions": {
                "YES": self.rest_yes_shares,
                "NO": self.rest_no_shares
            }
        }
        
        with open(out_path / "report.json", "w") as f:
            json.dump(report, f, indent=2)
        
        print("\n" + "=" * 60, flush=True)
        print("FINAL REPORT (V5 - OPENING MODE + P0+P1 FIXES)", flush=True)
        print("=" * 60, flush=True)
        print(f"Ticks: {self.metrics.ticks}", flush=True)
        print("-" * 40, flush=True)
        print(f"OPENING MODE entries: {self.opening_entries_posted}", flush=True)
        print(f"Total entries posted: {self.metrics.entries_posted}", flush=True)
        print(f"  Blocked (inventory): {self.metrics.entries_blocked_inventory}", flush=True)
        print(f"  Blocked (pending): {self.metrics.entries_blocked_pending}", flush=True)
        print(f"  Blocked (regime): {self.metrics.entries_blocked_regime}", flush=True)
        print(f"  Blocked (late): {self.metrics.entries_blocked_late}", flush=True)
        print("-" * 40, flush=True)
        print(f"Exits posted: {self.metrics.exits_posted}", flush=True)
        print(f"Exits repriced: {self.metrics.exits_repriced}", flush=True)
        print(f"Cancel-confirm cycles: {self.metrics.exit_cancel_confirm_cycles}", flush=True)
        print(f"Round trips: {self.metrics.round_trips}", flush=True)
        print("-" * 40, flush=True)
        print(f"Balance errors: {self.metrics.balance_errors}", flush=True)
        print(f"API errors: {self.metrics.api_errors}", flush=True)
        print("-" * 40, flush=True)
        print(f"Final positions: YES={self.rest_yes_shares:.2f} NO={self.rest_no_shares:.2f}", flush=True)
        print("=" * 60, flush=True)


def main():
    import argparse
    
    parser = argparse.ArgumentParser()
    parser.add_argument("--seconds", type=int, default=60)
    parser.add_argument("--outdir", default="mm_out")
    args = parser.parse_args()
    
    config = Config.from_env("pm_api_config.json")
    
    if os.environ.get("LIVE") == "1":
        config.mode = RunMode.LIVE
    
    from mm_bot.market import MarketResolver
    resolver = MarketResolver(config)
    market = resolver.resolve_market()
    
    if not market:
        print("[ERROR] Could not resolve market", flush=True)
        return
    
    print(f"[MARKET] {market.question}", flush=True)
    print(f"[MARKET] YES: {market.yes_token_id[:30]}...", flush=True)
    print(f"[MARKET] NO: {market.no_token_id[:30]}...", flush=True)
    print(f"[MARKET] Ends in: {market.time_str}", flush=True)
    
    runner = SafeRunnerV5(config, market.yes_token_id, market.no_token_id, market.end_time)
    runner.run(args.seconds, args.outdir)


if __name__ == "__main__":
    main()

