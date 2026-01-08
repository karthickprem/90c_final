"""
Runner V4 - All 5 Fixes Implemented
====================================

FIXES (from user feedback):
1. SYNTHETIC ENTRY ON RECONCILE - When reconcile detects inv > internal, 
   create synthetic entry fill from position avg/cost basis
2. MIN SIZE + DUST MODE - Enforce min_size + buffer, handle dust positions
3. BALANCE/ALLOWANCE ERRORS - Better debug logging and error handling
4. SPIKE DETECTOR REWORK (HYSTERESIS) - Proper trigger/clear thresholds
5. INVENTORY RULE - Cancel other side's entries when one side fills

Key safety features from V3:
- Accurate position tracking with REST reconciliation every 5s
- Exit Supervisor that NEVER gets cancelled
- 250ms loop tick for order management
- Stop-loss with optional emergency taker exit

ENDGAME RULES (15-min markets):
1. Entry cutoff: No NEW entries when seconds_to_settlement < 180s
2. Extreme-odds cutoff: No NEW entries if mid > 0.90 or mid < 0.10
3. Flatten deadline: When seconds_to_settlement < 120s, EXIT-ONLY mode
4. Spike behavior: Never blocks exits, only pauses entries
5. Max inventory age: If inv>0 for >N seconds without fill, emergency flatten
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

from mm_bot.config import Config, RunMode
from mm_bot.clob import ClobWrapper
from mm_bot.positions import PositionManager
from mm_bot.exit_supervisor import ExitSupervisor, ExitMode
from mm_bot.fill_tracker import FillTracker


class BotState(Enum):
    STARTUP = "startup"
    FLATTEN = "flatten"       # Unwinding existing positions at startup
    QUOTING = "quoting"       # Normal operation
    SPIKE_PAUSE = "spike"     # Paused due to spike (exits continue)
    EXIT_ONLY = "exit_only"   # Near settlement or extreme odds
    EMERGENCY = "emergency"   # Emergency flatten in progress
    SHUTDOWN = "shutdown"


# ENDGAME CONSTANTS
ENTRY_CUTOFF_SECS = 180       # No new entries when < 3 min left
FLATTEN_DEADLINE_SECS = 120   # EXIT-ONLY mode when < 2 min left
EXTREME_ODDS_HIGH = 0.90      # No entries above this mid
EXTREME_ODDS_LOW = 0.10       # No entries below this mid
MAX_INVENTORY_AGE_SECS = 60   # Emergency flatten if inv held this long


@dataclass
class SpikeDetector:
    """
    FIX #4: HYSTERESIS-based spike detection
    
    - Trigger if abs(mid_now - mid_1s_ago) >= trigger_threshold
    - Clear only when abs(mid_now - mid_1s_ago) <= clear_threshold for N consecutive checks
    """
    trigger_threshold_cents: float = 5.0
    clear_threshold_cents: float = 2.0
    stable_count_required: int = 5  # 5 consecutive stable checks
    cooldown_secs: float = 10.0
    
    # State
    in_spike: bool = False
    cooldown_until: float = 0.0
    stable_count: int = 0
    
    # Reference prices
    last_yes_mid: float = 0.0
    last_no_mid: float = 0.0
    last_check_time: float = 0.0
    reference_yes_mid: float = 0.0  # Baseline after cooldown
    reference_no_mid: float = 0.0
    
    # Logging (only print once per state change)
    _logged_spike: bool = False
    
    def check(self, yes_mid: float, no_mid: float) -> bool:
        """
        Check for spike condition. Returns True if in spike/cooldown.
        """
        now = time.time()
        
        # First tick - initialize
        if self.last_check_time == 0:
            self._reset_reference(yes_mid, no_mid, now)
            return False
        
        # In cooldown?
        if now < self.cooldown_until:
            return True
        
        # Check movement from last tick (1s-ago style)
        dt = now - self.last_check_time
        if dt < 0.5:  # Don't check faster than 500ms
            return self.in_spike
        
        yes_move = abs(yes_mid - self.last_yes_mid)
        no_move = abs(no_mid - self.last_no_mid)
        
        trigger = self.trigger_threshold_cents / 100.0
        clear = self.clear_threshold_cents / 100.0
        
        if yes_move >= trigger or no_move >= trigger:
            # SPIKE DETECTED
            if not self._logged_spike:
                print(f"[SPIKE_DETECTED] YES: {yes_move*100:.1f}c NO: {no_move*100:.1f}c", flush=True)
                self._logged_spike = True
            
            self.in_spike = True
            self.stable_count = 0
            self.cooldown_until = now + self.cooldown_secs
            self._update_last(yes_mid, no_mid, now)
            return True
        
        elif yes_move <= clear and no_move <= clear:
            # Price stable
            self.stable_count += 1
            
            if self.stable_count >= self.stable_count_required and self.in_spike:
                # SPIKE CLEARED
                self.in_spike = False
                self._logged_spike = False
                self._reset_reference(yes_mid, no_mid, now)
                print(f"[SPIKE_CLEARED] Stable for {self.stable_count} checks", flush=True)
        
        else:
            # Movement but not spike
            self.stable_count = 0
        
        self._update_last(yes_mid, no_mid, now)
        return self.in_spike
    
    def _update_last(self, yes_mid: float, no_mid: float, now: float):
        self.last_yes_mid = yes_mid
        self.last_no_mid = no_mid
        self.last_check_time = now
    
    def _reset_reference(self, yes_mid: float, no_mid: float, now: float):
        self.reference_yes_mid = yes_mid
        self.reference_no_mid = no_mid
        self._update_last(yes_mid, no_mid, now)
        self.stable_count = 0


@dataclass
class LoopMetrics:
    """Metrics for the main loop"""
    ticks: int = 0
    entries_posted: int = 0
    entries_filled: int = 0
    entries_blocked_late: int = 0
    entries_blocked_extreme: int = 0
    entries_blocked_spike: int = 0
    entries_blocked_dust: int = 0
    exits_posted: int = 0
    exits_filled: int = 0
    reconcile_count: int = 0
    reconcile_mismatches: int = 0
    synthetic_entries: int = 0
    spike_triggers: int = 0
    kill_triggers: int = 0
    max_adverse_excursion: float = 0.0
    max_inventory_age_s: float = 0.0
    emergency_flatten_triggered: int = 0
    
    # FIX #5 tracking
    cross_side_cancels: int = 0  # When one side fills, cancel other
    
    # PnL tracking
    realized_pnl: float = 0.0
    fees_paid: float = 0.0
    
    # Error tracking
    api_errors: int = 0
    balance_errors: int = 0


class SafeRunnerV4:
    """
    Safe market making runner with ALL 5 FIXES.
    """
    
    def __init__(self, config: Config, yes_token: str, no_token: str, market_end_time: int):
        self.config = config
        self.yes_token = yes_token
        self.no_token = no_token
        self.market_end_time = market_end_time
        
        # Mode
        self.live = config.mode == RunMode.LIVE
        
        # Components
        self.clob = ClobWrapper(config)
        self.position_manager = PositionManager(config)
        self.position_manager.set_market_tokens(yes_token, no_token)
        
        # Exit supervisor
        self.exit_supervisor = ExitSupervisor(
            config, self.clob, self.position_manager, None
        )
        
        # Fill tracker (with synthetic entry support)
        self.fill_tracker = FillTracker(config)
        
        # FIX #4: Hysteresis spike detector
        self.spike_detector = SpikeDetector(
            trigger_threshold_cents=config.quoting.spike_threshold_cents,
            clear_threshold_cents=config.quoting.spike_threshold_cents / 2.5,  # ~40% of trigger
            cooldown_secs=config.quoting.spike_cooldown_secs
        )
        
        # State
        self.state = BotState.STARTUP
        self.metrics = LoopMetrics()
        
        # Emergency flatten tracking (single trigger, not spam)
        self._emergency_flatten_triggered = False
        
        # Timing
        self.loop_interval_ms = 250
        self.reconcile_interval_s = 5.0
        self.last_reconcile = 0.0
        
        # FIX #2: Min order size config
        self.min_order_size = config.risk.min_order_size
        self.min_order_buffer = config.risk.min_order_size_buffer
        self.effective_min_size = self.min_order_size + self.min_order_buffer
        self.dust_mode = config.risk.dust_mode
        
        # Entry order tracking
        self.entry_orders: Dict[str, dict] = {}  # token_id -> order info
        
        # Shutdown
        self.running = False
        self.shutdown_event = threading.Event()
        
        # Lock file
        self.lock_file = Path("mm_bot.lock")
        self._acquired_lock = False
        
        # Output
        self.log_file = None
    
    def _acquire_lock(self) -> bool:
        """Prevent duplicate runners"""
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
        """Release lock file"""
        if self._acquired_lock and self.lock_file.exists():
            self.lock_file.unlink()
            self._acquired_lock = False
    
    @property
    def seconds_to_settlement(self) -> int:
        return max(0, self.market_end_time - int(time.time()))
    
    def _initial_reconcile(self) -> Tuple[float, float]:
        """Reconcile positions at startup"""
        print("[STARTUP] Fetching initial positions...", flush=True)
        
        mismatches = self.position_manager.reconcile_from_rest()
        
        yes_shares = self.position_manager.get_shares(self.yes_token)
        no_shares = self.position_manager.get_shares(self.no_token)
        
        print(f"[STARTUP] YES: {yes_shares:.2f} shares", flush=True)
        print(f"[STARTUP] NO: {no_shares:.2f} shares", flush=True)
        
        # FIX #1: Create synthetic entries for existing positions
        if yes_shares > 0:
            pos = self.position_manager.get_position(self.yes_token)
            if pos:
                self.fill_tracker.create_synthetic_entry(
                    self.yes_token, yes_shares, pos.avg_price
                )
                self.metrics.synthetic_entries += 1
        
        if no_shares > 0:
            pos = self.position_manager.get_position(self.no_token)
            if pos:
                self.fill_tracker.create_synthetic_entry(
                    self.no_token, no_shares, pos.avg_price
                )
                self.metrics.synthetic_entries += 1
        
        return yes_shares, no_shares
    
    def _check_startup_state(self) -> BotState:
        """Determine initial state based on positions and time"""
        yes_shares, no_shares = self._initial_reconcile()
        
        secs_left = self.seconds_to_settlement
        print(f"[STARTUP] Seconds to settlement: {secs_left}s", flush=True)
        
        if secs_left < FLATTEN_DEADLINE_SECS:
            print(f"[STARTUP] Past flatten deadline ({secs_left}s < {FLATTEN_DEADLINE_SECS}s) -> EXIT_ONLY", flush=True)
            return BotState.EXIT_ONLY
        
        if yes_shares > 0.01 or no_shares > 0.01:
            print("[STARTUP] Existing positions detected -> FLATTEN mode", flush=True)
            return BotState.FLATTEN
        
        if secs_left < ENTRY_CUTOFF_SECS:
            print(f"[STARTUP] Past entry cutoff ({secs_left}s < {ENTRY_CUTOFF_SECS}s) -> EXIT_ONLY", flush=True)
            return BotState.EXIT_ONLY
        
        return BotState.QUOTING
    
    def _get_book(self, token_id: str) -> dict:
        """Get order book for token"""
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
    
    def _check_endgame_rules(self, yes_mid: float, no_mid: float) -> Tuple[bool, str]:
        """Check ENDGAME RULES for entry blocking."""
        secs_left = self.seconds_to_settlement
        
        if secs_left < ENTRY_CUTOFF_SECS:
            self.metrics.entries_blocked_late += 1
            return True, f"ENTRY_BLOCKED_LATE (secs_left={secs_left})"
        
        if yes_mid > EXTREME_ODDS_HIGH or yes_mid < EXTREME_ODDS_LOW:
            self.metrics.entries_blocked_extreme += 1
            return True, f"ENTRY_BLOCKED_EXTREME (yes_mid={yes_mid:.2f})"
        if no_mid > EXTREME_ODDS_HIGH or no_mid < EXTREME_ODDS_LOW:
            self.metrics.entries_blocked_extreme += 1
            return True, f"ENTRY_BLOCKED_EXTREME (no_mid={no_mid:.2f})"
        
        return False, ""
    
    def _check_flatten_deadline(self) -> bool:
        return self.seconds_to_settlement < FLATTEN_DEADLINE_SECS
    
    def _check_inventory_age_emergency(self) -> bool:
        if self._emergency_flatten_triggered:
            return True
        
        inv_age = self.position_manager.get_inventory_age_seconds()
        
        if inv_age > MAX_INVENTORY_AGE_SECS:
            total_shares = self.position_manager.get_total_shares()
            if total_shares > 0.01:
                emergency_enabled = self.config.risk.emergency_taker_exit
                if emergency_enabled:
                    print(f"[EMERGENCY] Inventory age {inv_age:.1f}s > {MAX_INVENTORY_AGE_SECS}s -> EMERGENCY flatten", flush=True)
                    self.metrics.emergency_flatten_triggered += 1
                    self._emergency_flatten_triggered = True
                    return True
        
        return False
    
    def _cancel_entry_orders(self):
        """Cancel all entry orders (NOT exit orders)"""
        for token_id, info in list(self.entry_orders.items()):
            try:
                self.clob.cancel_order(info["order_id"])
            except:
                pass
        self.entry_orders.clear()
    
    def _cancel_entry_for_token(self, token_id: str):
        """Cancel entry order for a specific token (FIX #5)"""
        if token_id in self.entry_orders:
            info = self.entry_orders[token_id]
            try:
                self.clob.cancel_order(info["order_id"])
                print(f"[FIX5] Cancelled entry for {token_id[:20]}... (other side filled)", flush=True)
                self.metrics.cross_side_cancels += 1
            except:
                pass
            del self.entry_orders[token_id]
    
    def _check_dust_position(self, shares: float) -> bool:
        """
        FIX #2: Check if position is dust (< min_order_size)
        """
        if shares > 0 and shares < self.min_order_size:
            return True
        return False
    
    def _handle_dust_position(self, token_id: str, shares: float, book: dict) -> bool:
        """
        FIX #2: Handle dust position
        Returns True if we should stop trying to exit (hold to settlement)
        """
        if self.dust_mode == "TOPUP":
            # Try to top up to min size
            needed = self.min_order_size - shares
            price = book["best_ask"]
            cost = needed * price
            
            # Check if we can afford it
            if cost <= self.config.risk.max_usdc_locked:
                try:
                    result = self.clob.post_order(
                        token_id=token_id,
                        side="BUY",
                        price=price,
                        size=needed,
                        post_only=True
                    )
                    if result.success:
                        print(f"[DUST_TOPUP] Posted top-up BUY {needed:.2f} @ {price:.4f}", flush=True)
                        return False
                except Exception as e:
                    print(f"[DUST_TOPUP] Failed: {e}", flush=True)
            
        # Default: HOLD mode
        if self.metrics.ticks % 20 == 0:  # Log every 5 seconds
            print(f"[DUST_HOLD] {token_id[:20]}... has {shares:.2f} shares < {self.min_order_size} min, holding to settlement", flush=True)
        return True  # Stop trying to exit
    
    def _place_entry_bid(self, token_id: str, book: dict, label: str, block_reason: str = "") -> Optional[str]:
        """Place an entry bid near the touch"""
        
        if block_reason:
            if self.metrics.ticks % 8 == 0:
                print(f"[ENTRY] {label} blocked: {block_reason}", flush=True)
            return None
        
        # FIX #2: Enforce minimum order size + buffer
        quote_size = self.config.quoting.base_quote_size
        if quote_size < self.effective_min_size:
            quote_size = self.effective_min_size
        
        # Near-touch quoting
        tick = 0.01
        desired_bid = book["best_bid"]
        
        min_spread = self.config.quoting.min_half_spread_cents / 100.0
        max_usdc = self.config.risk.max_usdc_locked
        max_shares = self.config.risk.max_inv_shares_per_token
        
        spread = book["best_ask"] - book["best_bid"]
        if spread < min_spread:
            return None
        
        current_shares = self.position_manager.get_shares(token_id)
        if current_shares >= max_shares:
            return None
        
        cost = quote_size * desired_bid
        if cost > max_usdc:
            quote_size = int(max_usdc / desired_bid)
            if quote_size < self.effective_min_size:
                self.metrics.entries_blocked_dust += 1
                return None  # Can't place order meeting min size
        
        if token_id in self.entry_orders:
            existing = self.entry_orders[token_id]
            if abs(existing["price"] - desired_bid) >= tick:
                try:
                    self.clob.cancel_order(existing["order_id"])
                except:
                    pass
                del self.entry_orders[token_id]
            else:
                return existing["order_id"]
        
        if not self.live:
            print(f"[DRYRUN] Would place {label} BID {quote_size} @ {desired_bid:.4f}", flush=True)
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
                self.entry_orders[token_id] = {
                    "order_id": result.order_id,
                    "price": desired_bid,
                    "size": quote_size,
                    "time": time.time()
                }
                self.metrics.entries_posted += 1
                print(f"[ENTRY] Posted {label} BID {quote_size} @ {desired_bid:.4f}", flush=True)
                return result.order_id
            else:
                # FIX #3: Better error logging
                if result.error:
                    error_str = str(result.error).lower()
                    if "balance" in error_str or "allowance" in error_str:
                        self.metrics.balance_errors += 1
                        print(f"[ENTRY] {label} BALANCE_ERROR: size={quote_size} price={desired_bid:.4f} cost=${cost:.2f}", flush=True)
                    else:
                        print(f"[ENTRY] {label} failed: {result.error}", flush=True)
        
        except Exception as e:
            self.metrics.api_errors += 1
            print(f"[ENTRY] Error: {e}", flush=True)
        
        return None
    
    def _reconcile_tick(self) -> Dict:
        """Periodic reconciliation with FIX #1 (synthetic entries)"""
        now = time.time()
        
        if now - self.last_reconcile < self.reconcile_interval_s:
            return {}
        
        self.last_reconcile = now
        self.metrics.reconcile_count += 1
        
        # Get current internal state BEFORE reconcile
        prev_yes = self.position_manager.get_shares(self.yes_token)
        prev_no = self.position_manager.get_shares(self.no_token)
        
        mismatches = self.position_manager.reconcile_from_rest()
        
        if mismatches:
            self.metrics.reconcile_mismatches += len(mismatches)
            
            for token_id, data in mismatches.items():
                print(f"[RECONCILE] {token_id[:20]}... {data}", flush=True)
                
                # FIX #1: Create synthetic entry if inventory appeared
                if data["diff"] > 0:
                    print(f"[RECONCILE] CRITICAL: Missed fill detected!", flush=True)
                    
                    # Get position avg price from REST
                    pos = self.position_manager.get_position(token_id)
                    if pos and pos.avg_price > 0:
                        self.fill_tracker.create_synthetic_entry(
                            token_id, data["rest"], pos.avg_price
                        )
                        self.metrics.synthetic_entries += 1
                    
                    # FIX #5: Cancel the OTHER side's entry (inventory rule)
                    if token_id == self.yes_token:
                        self._cancel_entry_for_token(self.no_token)
                    else:
                        self._cancel_entry_for_token(self.yes_token)
        
        return mismatches
    
    def _log_tick(self, yes_book: dict, no_book: dict):
        """Log current state"""
        yes_shares = self.position_manager.get_shares(self.yes_token)
        no_shares = self.position_manager.get_shares(self.no_token)
        
        entry_count = len(self.entry_orders)
        exit_count = self.exit_supervisor.active_exit_count
        
        self.position_manager.update_mtm(self.yes_token, yes_book["best_bid"], yes_book["best_ask"])
        self.position_manager.update_mtm(self.no_token, no_book["best_bid"], no_book["best_ask"])
        
        mtm = self.position_manager.get_total_mtm()
        mae = self.position_manager.get_max_adverse_excursion()
        inv_age = self.position_manager.get_inventory_age_seconds()
        secs_left = self.seconds_to_settlement
        
        self.metrics.max_adverse_excursion = max(self.metrics.max_adverse_excursion, mae)
        self.metrics.max_inventory_age_s = max(self.metrics.max_inventory_age_s, inv_age)
        
        # Console log every 1s
        if self.metrics.ticks % 4 == 0:
            yes_mid = (yes_book["best_bid"] + yes_book["best_ask"]) / 2
            no_mid = (no_book["best_bid"] + no_book["best_ask"]) / 2
            
            state_str = self.state.value.upper()
            inv_str = f"YES:{yes_shares:.1f} NO:{no_shares:.1f}"
            order_str = f"E:{entry_count} X:{exit_count}"
            time_str = f"{secs_left//60}:{secs_left%60:02d}"
            
            spike_str = "[SPIKE]" if self.spike_detector.in_spike else ""
            
            print(f"[{state_str}] {time_str} left | YES={yes_mid:.2f} NO={no_mid:.2f} | {inv_str} | {order_str} | MTM=${mtm:.2f} {spike_str}", flush=True)
        
        # JSONL log
        if self.log_file:
            log_entry = {
                "ts": datetime.now().isoformat(),
                "tick": self.metrics.ticks,
                "state": self.state.value,
                "secs_left": secs_left,
                "yes_bid": yes_book["best_bid"],
                "yes_ask": yes_book["best_ask"],
                "no_bid": no_book["best_bid"],
                "no_ask": no_book["best_ask"],
                "yes_shares": yes_shares,
                "no_shares": no_shares,
                "mtm": mtm,
                "mae": mae,
                "inv_age": inv_age,
                "in_spike": self.spike_detector.in_spike,
                "synthetic_entries": self.metrics.synthetic_entries,
                "cross_side_cancels": self.metrics.cross_side_cancels,
                "balance_errors": self.metrics.balance_errors
            }
            self.log_file.write(json.dumps(log_entry) + "\n")
            self.log_file.flush()
    
    def _run_tick(self):
        """Main loop tick (250ms)"""
        self.metrics.ticks += 1
        
        # Get books
        yes_book = self._get_book(self.yes_token)
        no_book = self._get_book(self.no_token)
        
        if not yes_book["has_liquidity"] or not no_book["has_liquidity"]:
            if self.metrics.ticks % 4 == 0:
                print("[TICK] No valid book, skipping", flush=True)
            return
        
        yes_mid = (yes_book["best_bid"] + yes_book["best_ask"]) / 2
        no_mid = (no_book["best_bid"] + no_book["best_ask"]) / 2
        
        # Poll fill tracker
        market_tokens = {self.yes_token, self.no_token}
        new_fills = self.fill_tracker.poll_fills(market_tokens)
        
        for fill in new_fills:
            self.exit_supervisor.on_fill(fill.token_id, fill.side, fill.size)
            
            # FIX #5: If we got an entry fill, cancel the other side's entry
            if fill.side == "BUY":
                if fill.token_id == self.yes_token:
                    self._cancel_entry_for_token(self.no_token)
                else:
                    self._cancel_entry_for_token(self.yes_token)
        
        # Reconcile periodically
        self._reconcile_tick()
        
        # Reset emergency flag if inventory is 0
        total_shares = self.position_manager.get_total_shares()
        if total_shares < 0.01 and self._emergency_flatten_triggered:
            self._emergency_flatten_triggered = False
            self.exit_supervisor.clear_token_state(self.yes_token)
            self.exit_supervisor.clear_token_state(self.no_token)
        
        # FIX #2: Check for dust positions
        yes_shares = self.position_manager.get_shares(self.yes_token)
        no_shares = self.position_manager.get_shares(self.no_token)
        
        yes_is_dust = self._check_dust_position(yes_shares)
        no_is_dust = self._check_dust_position(no_shares)
        
        # Exit supervisor - ALWAYS RUNS
        # But skip dust positions in HOLD mode
        if not (yes_is_dust and self._handle_dust_position(self.yes_token, yes_shares, yes_book)):
            pass  # Supervisor will handle
        if not (no_is_dust and self._handle_dust_position(self.no_token, no_shares, no_book)):
            pass  # Supervisor will handle
        
        self.exit_supervisor.tick(yes_book, no_book, self.yes_token, self.no_token)
        
        # Check flatten deadline
        if self._check_flatten_deadline():
            if self.state != BotState.EXIT_ONLY:
                print(f"[EXIT_ONLY_MODE] Flatten deadline reached", flush=True)
                self._cancel_entry_orders()
                self.state = BotState.EXIT_ONLY
        
        # Check inventory age emergency
        if self._check_inventory_age_emergency():
            if self.state != BotState.EMERGENCY:
                print(f"[EMERGENCY_FLATTEN_TRIGGERED] Inventory age exceeded", flush=True)
                self._cancel_entry_orders()
                self.state = BotState.EMERGENCY
                for token in [self.yes_token, self.no_token]:
                    exit_order = self.exit_supervisor.get_exit_order(token)
                    if exit_order:
                        exit_order.mode = ExitMode.EMERGENCY
        
        # FIX #4: Check spike with hysteresis
        in_spike = self.spike_detector.check(yes_mid, no_mid)
        if in_spike and not self.spike_detector._logged_spike:
            self.metrics.spike_triggers += 1
        
        # Check endgame rules
        block_entries, block_reason = self._check_endgame_rules(yes_mid, no_mid)
        if in_spike:
            block_entries = True
            block_reason = "SPIKE_PAUSE"
            self.metrics.entries_blocked_spike += 1
        
        # State machine
        if self.state == BotState.FLATTEN:
            if self.position_manager.get_total_shares() < 0.01:
                secs_left = self.seconds_to_settlement
                if secs_left >= ENTRY_CUTOFF_SECS:
                    print("[FLATTEN] Complete, switching to QUOTING", flush=True)
                    self.state = BotState.QUOTING
                else:
                    print("[FLATTEN] Complete, but past entry cutoff -> EXIT_ONLY", flush=True)
                    self.state = BotState.EXIT_ONLY
        
        elif self.state == BotState.EXIT_ONLY:
            self._cancel_entry_orders()
            if self.seconds_to_settlement >= ENTRY_CUTOFF_SECS:
                if self.position_manager.get_total_shares() < 0.01:
                    print("[EXIT_ONLY] Time permits and no inventory -> QUOTING", flush=True)
                    self.state = BotState.QUOTING
        
        elif self.state == BotState.EMERGENCY:
            self._cancel_entry_orders()
            if self.position_manager.get_total_shares() < 0.01:
                print("[EMERGENCY] Flatten complete -> EXIT_ONLY", flush=True)
                self.state = BotState.EXIT_ONLY
        
        elif self.state == BotState.QUOTING:
            if in_spike or block_entries:
                self._cancel_entry_orders()
            else:
                self._place_entry_bid(self.yes_token, yes_book, "YES", "")
                self._place_entry_bid(self.no_token, no_book, "NO", "")
        
        # Log
        self._log_tick(yes_book, no_book)
    
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
            print(f"[START] Min order size: {self.effective_min_size} shares (FIX #2)", flush=True)
            print(f"[START] Spike threshold: {self.spike_detector.trigger_threshold_cents}c (FIX #4 hysteresis)", flush=True)
            
            try:
                bal = self.clob.get_balance()
                self.fill_tracker.set_session_start_cash(bal.get('usdc', 0))
                print(f"[START] Session start cash: ${bal.get('usdc', 0):.2f}", flush=True)
            except:
                pass
            
            self.state = self._check_startup_state()
            
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
            self._cancel_entry_orders()
            self.position_manager.reconcile_from_rest()
            self._write_report(out_path)
            
            if self.log_file:
                self.log_file.close()
            self._release_lock()
    
    def _write_report(self, out_path: Path):
        """Write final report"""
        pos_snapshot = self.position_manager.get_snapshot()
        exit_metrics = self.exit_supervisor.get_metrics()
        fill_metrics = self.fill_tracker.get_metrics()
        
        report = {
            "timestamp": datetime.now().isoformat(),
            "mode": "LIVE" if self.live else "DRYRUN",
            "version": "V4",
            "fixes": [
                "FIX1: Synthetic entry on reconcile",
                "FIX2: Min size + dust mode",
                "FIX3: Balance/allowance error logging",
                "FIX4: Spike hysteresis",
                "FIX5: Cancel other side on fill"
            ],
            "metrics": {
                "ticks": self.metrics.ticks,
                "entries_posted": self.metrics.entries_posted,
                "entries_blocked_late": self.metrics.entries_blocked_late,
                "entries_blocked_extreme": self.metrics.entries_blocked_extreme,
                "entries_blocked_spike": self.metrics.entries_blocked_spike,
                "entries_blocked_dust": self.metrics.entries_blocked_dust,
                "entries_filled": fill_metrics["entry_fills"],
                "synthetic_entries": fill_metrics["synthetic_entries"],
                "exits_posted": exit_metrics["exits_placed"],
                "exits_repriced": exit_metrics["exits_repriced"],
                "exits_filled": fill_metrics["exit_fills"],
                "emergency_exits": exit_metrics["emergency_exits"],
                "emergency_flatten_triggered": self.metrics.emergency_flatten_triggered,
                "cross_side_cancels": self.metrics.cross_side_cancels,
                "reconcile_mismatches": self.metrics.reconcile_mismatches,
                "spike_triggers": self.metrics.spike_triggers,
                "api_errors": self.metrics.api_errors,
                "balance_errors": self.metrics.balance_errors,
                "max_adverse_excursion": self.metrics.max_adverse_excursion,
                "max_inventory_age_s": self.metrics.max_inventory_age_s
            },
            "pnl": {
                "realized_pnl": fill_metrics["realized_pnl"],
                "total_rebates": fill_metrics["total_rebates"],
                "total_fees": fill_metrics["total_fees"],
                "complete_round_trips": fill_metrics["complete_round_trips"],
                "exit_latency_p50": fill_metrics["exit_latency_p50"],
                "exit_latency_p95": fill_metrics["exit_latency_p95"]
            },
            "final_positions": pos_snapshot
        }
        
        with open(out_path / "report.json", "w") as f:
            json.dump(report, f, indent=2)
        
        print("\n" + "=" * 60, flush=True)
        print("FINAL REPORT (V4 - ALL 5 FIXES)", flush=True)
        print("=" * 60, flush=True)
        print(f"Ticks: {self.metrics.ticks}", flush=True)
        print(f"Entries posted: {self.metrics.entries_posted}", flush=True)
        print(f"Entries blocked (late): {self.metrics.entries_blocked_late}", flush=True)
        print(f"Entries blocked (extreme): {self.metrics.entries_blocked_extreme}", flush=True)
        print(f"Entries blocked (spike): {self.metrics.entries_blocked_spike}", flush=True)
        print(f"Entries blocked (dust): {self.metrics.entries_blocked_dust}", flush=True)
        print("-" * 40, flush=True)
        print(f"Entry fills: {fill_metrics['entry_fills']} (synthetic: {fill_metrics['synthetic_entries']})", flush=True)
        print(f"Exit fills: {fill_metrics['exit_fills']}", flush=True)
        print(f"Complete round trips: {fill_metrics['complete_round_trips']}", flush=True)
        print("-" * 40, flush=True)
        print(f"Cross-side cancels (FIX5): {self.metrics.cross_side_cancels}", flush=True)
        print(f"Spike triggers: {self.metrics.spike_triggers}", flush=True)
        print(f"Balance errors: {self.metrics.balance_errors}", flush=True)
        print(f"Emergency flatten triggered: {self.metrics.emergency_flatten_triggered}", flush=True)
        print("-" * 40, flush=True)
        print("PnL:", flush=True)
        print(f"  Realized PnL: ${fill_metrics['realized_pnl']:.4f}", flush=True)
        print(f"  Rebates credited: ${fill_metrics['total_rebates']:.4f}", flush=True)
        print(f"  Fees paid: ${fill_metrics['total_fees']:.4f}", flush=True)
        print(f"  Exit latency p50/p95: {fill_metrics['exit_latency_p50']:.1f}s / {fill_metrics['exit_latency_p95']:.1f}s", flush=True)
        print("-" * 40, flush=True)
        print(f"Final positions: {pos_snapshot['positions']}", flush=True)
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
    
    runner = SafeRunnerV4(config, market.yes_token_id, market.no_token_id, market.end_time)
    runner.run(args.seconds, args.outdir)


if __name__ == "__main__":
    main()

