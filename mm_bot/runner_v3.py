"""
Runner V3 - Safe Market Making with ENDGAME RULES
===================================================

Key safety features:
1. Accurate position tracking with REST reconciliation every 5s
2. Exit Supervisor that NEVER gets cancelled
3. 250ms loop tick for order management
4. Stop-loss with optional emergency taker exit

ENDGAME RULES (15-min markets):
1. Entry cutoff: No NEW entries when seconds_to_settlement < 180s
2. Extreme-odds cutoff: No NEW entries if mid > 0.95 or mid < 0.05
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
from typing import Optional, Dict, List, Tuple
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


# ENDGAME CONSTANTS (tightened per user feedback)
ENTRY_CUTOFF_SECS = 180       # No new entries when < 3 min left
FLATTEN_DEADLINE_SECS = 120   # EXIT-ONLY mode when < 2 min left
EXTREME_ODDS_HIGH = 0.90      # No entries above this mid (was 0.95 - too late)
EXTREME_ODDS_LOW = 0.10       # No entries below this mid (was 0.05 - too late)
MAX_INVENTORY_AGE_SECS = 60   # Emergency flatten if inv held this long
FORCED_TAKER_FLATTEN_SECS = 60  # Force taker exit in last 60s if inventory exists


@dataclass
class LoopMetrics:
    """Metrics for the main loop"""
    ticks: int = 0
    entries_posted: int = 0
    entries_filled: int = 0
    entries_blocked_late: int = 0
    entries_blocked_extreme: int = 0
    exits_posted: int = 0
    exits_filled: int = 0
    reconcile_count: int = 0
    reconcile_mismatches: int = 0
    spike_pauses: int = 0
    kill_triggers: int = 0
    max_adverse_excursion: float = 0.0
    max_inventory_age_s: float = 0.0
    ws_disconnects: int = 0
    emergency_flatten_triggered: int = 0
    
    # PnL tracking
    realized_pnl: float = 0.0
    fees_paid: float = 0.0
    
    # Error tracking
    api_errors: int = 0
    exit_failures: int = 0


class SafeRunner:
    """
    Safe market making runner with ENDGAME RULES.
    """
    
    def __init__(self, config: Config, yes_token: str, no_token: str, market_end_time: int):
        self.config = config
        self.yes_token = yes_token
        self.no_token = no_token
        self.market_end_time = market_end_time  # Unix timestamp
        
        # Mode
        self.live = config.mode == RunMode.LIVE
        
        # Components
        self.clob = ClobWrapper(config)
        self.position_manager = PositionManager(config)
        self.position_manager.set_market_tokens(yes_token, no_token)
        
        # Exit supervisor - the key safety component
        self.exit_supervisor = ExitSupervisor(
            config, self.clob, self.position_manager, None
        )
        
        # Fill tracker - accurate fill counting and PnL
        self.fill_tracker = FillTracker(config)
        
        # State
        self.state = BotState.STARTUP
        self.metrics = LoopMetrics()
        
        # Emergency flatten tracking (single trigger, not spam)
        self._emergency_flatten_triggered = False
        
        # Timing
        self.loop_interval_ms = 250  # 250ms loop ticks
        self.reconcile_interval_s = 5.0
        self.last_reconcile = 0.0
        
        # Spike detection
        self.last_mid_yes = 0.0
        self.last_mid_no = 0.0
        self.last_mid_time = 0.0
        self.spike_cooldown_until = 0.0
        self.spike_threshold_cents = getattr(config.quoting, 'spike_threshold_cents', 2)
        self.spike_window_secs = getattr(config.quoting, 'spike_window_secs', 5)
        self.spike_cooldown_secs = getattr(config.quoting, 'spike_cooldown_secs', 10)
        
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
                # Check if process exists
                try:
                    import psutil
                    if psutil.pid_exists(pid):
                        print(f"[SAFETY] Another runner is active (PID {pid})", flush=True)
                        return False
                except ImportError:
                    pass  # psutil not available, proceed
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
        """Get seconds remaining until market settlement"""
        return max(0, self.market_end_time - int(time.time()))
    
    def _initial_reconcile(self) -> Tuple[float, float]:
        """Reconcile positions at startup"""
        print("[STARTUP] Fetching initial positions...", flush=True)
        
        mismatches = self.position_manager.reconcile_from_rest()
        
        yes_shares = self.position_manager.get_shares(self.yes_token)
        no_shares = self.position_manager.get_shares(self.no_token)
        
        print(f"[STARTUP] YES: {yes_shares:.2f} shares", flush=True)
        print(f"[STARTUP] NO: {no_shares:.2f} shares", flush=True)
        
        if mismatches:
            for token, data in mismatches.items():
                print(f"[STARTUP] MISMATCH: {token[:20]}... {data}", flush=True)
        
        return yes_shares, no_shares
    
    def _check_startup_state(self) -> BotState:
        """Determine initial state based on positions and time"""
        yes_shares, no_shares = self._initial_reconcile()
        
        secs_left = self.seconds_to_settlement
        print(f"[STARTUP] Seconds to settlement: {secs_left}s", flush=True)
        
        # Check if already past flatten deadline
        if secs_left < FLATTEN_DEADLINE_SECS:
            print(f"[STARTUP] Past flatten deadline ({secs_left}s < {FLATTEN_DEADLINE_SECS}s) -> EXIT_ONLY mode", flush=True)
            return BotState.EXIT_ONLY
        
        # Check if existing positions
        if yes_shares > 0.01 or no_shares > 0.01:
            print("[STARTUP] Existing positions detected -> FLATTEN mode", flush=True)
            return BotState.FLATTEN
        
        # Check if past entry cutoff
        if secs_left < ENTRY_CUTOFF_SECS:
            print(f"[STARTUP] Past entry cutoff ({secs_left}s < {ENTRY_CUTOFF_SECS}s) -> EXIT_ONLY mode", flush=True)
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
    
    def _check_spike(self, yes_book: dict, no_book: dict) -> bool:
        """Check for spike condition - NEVER blocks exits"""
        now = time.time()
        
        # In cooldown?
        if now < self.spike_cooldown_until:
            return True
        
        yes_mid = (yes_book["best_bid"] + yes_book["best_ask"]) / 2
        no_mid = (no_book["best_bid"] + no_book["best_ask"]) / 2
        
        # First tick
        if self.last_mid_time == 0:
            self.last_mid_yes = yes_mid
            self.last_mid_no = no_mid
            self.last_mid_time = now
            return False
        
        # Check movement since last check
        dt = now - self.last_mid_time
        if dt >= self.spike_window_secs:
            yes_move = abs(yes_mid - self.last_mid_yes)
            no_move = abs(no_mid - self.last_mid_no)
            
            threshold = self.spike_threshold_cents / 100.0
            
            if yes_move >= threshold or no_move >= threshold:
                self.spike_cooldown_until = now + self.spike_cooldown_secs
                self.metrics.spike_pauses += 1
                print(f"[SPIKE] Move {yes_move*100:.1f}c / {no_move*100:.1f}c -> pause entries {self.spike_cooldown_secs}s (exits continue)", flush=True)
                return True
            
            # Update baseline
            self.last_mid_yes = yes_mid
            self.last_mid_no = no_mid
            self.last_mid_time = now
        
        return False
    
    def _check_endgame_rules(self, yes_mid: float, no_mid: float) -> Tuple[bool, str]:
        """
        Check ENDGAME RULES for entry blocking.
        
        Returns: (should_block_entries, reason)
        """
        secs_left = self.seconds_to_settlement
        
        # Rule 1: Entry cutoff (late window)
        if secs_left < ENTRY_CUTOFF_SECS:
            self.metrics.entries_blocked_late += 1
            return True, f"ENTRY_BLOCKED_LATE (secs_left={secs_left})"
        
        # Rule 2: Extreme odds cutoff
        if yes_mid > EXTREME_ODDS_HIGH or yes_mid < EXTREME_ODDS_LOW:
            self.metrics.entries_blocked_extreme += 1
            return True, f"ENTRY_BLOCKED_EXTREME (yes_mid={yes_mid:.2f})"
        if no_mid > EXTREME_ODDS_HIGH or no_mid < EXTREME_ODDS_LOW:
            self.metrics.entries_blocked_extreme += 1
            return True, f"ENTRY_BLOCKED_EXTREME (no_mid={no_mid:.2f})"
        
        return False, ""
    
    def _check_flatten_deadline(self) -> bool:
        """Check if past flatten deadline - triggers EXIT-ONLY mode"""
        secs_left = self.seconds_to_settlement
        return secs_left < FLATTEN_DEADLINE_SECS
    
    def _check_inventory_age_emergency(self) -> bool:
        """Check if inventory age exceeds threshold - triggers emergency flatten (ONCE, not spam)"""
        # Already in emergency - don't spam
        if self._emergency_flatten_triggered:
            return True
        
        inv_age = self.position_manager.get_inventory_age_seconds()
        
        if inv_age > MAX_INVENTORY_AGE_SECS:
            total_shares = self.position_manager.get_total_shares()
            if total_shares > 0.01:
                emergency_enabled = getattr(self.config.risk, 'emergency_taker_exit', False)
                if emergency_enabled:
                    print(f"[EMERGENCY] Inventory age {inv_age:.1f}s > {MAX_INVENTORY_AGE_SECS}s -> EMERGENCY flatten (triggered ONCE)", flush=True)
                    self.metrics.emergency_flatten_triggered += 1
                    self._emergency_flatten_triggered = True  # Only trigger once
                    return True
        
        return False
    
    def _cancel_entry_orders(self):
        """Cancel all entry orders (NOT exit orders)"""
        for token_id, info in list(self.entry_orders.items()):
            try:
                self.clob.cancel_order(info["order_id"])
                print(f"[ENTRY] Cancelled: {token_id[:20]}...", flush=True)
            except Exception as e:
                pass
        self.entry_orders.clear()
    
    def _place_entry_bid(self, token_id: str, book: dict, label: str, block_reason: str = "") -> Optional[str]:
        """Place an entry bid near the touch"""
        
        # Check if entries are blocked
        if block_reason:
            if self.metrics.ticks % 8 == 0:  # Log every 2s
                print(f"[ENTRY] {label} blocked: {block_reason}", flush=True)
            return None
        
        # Near-touch quoting: best_bid (join the bid)
        tick = 0.01
        desired_bid = book["best_bid"]
        
        # Get config
        min_spread = getattr(self.config.quoting, 'min_half_spread_cents', 1) / 100.0
        quote_size = getattr(self.config.quoting, 'base_quote_size', 5)
        max_usdc = getattr(self.config.risk, 'max_usdc_locked', 3)
        max_shares = getattr(self.config.risk, 'max_inv_shares_per_token', 50)
        
        # Check spread
        spread = book["best_ask"] - book["best_bid"]
        if spread < min_spread:
            return None
        
        # Check if we already have inventory (don't add more)
        current_shares = self.position_manager.get_shares(token_id)
        if current_shares >= max_shares:
            return None
        
        # Check USDC limit
        cost = quote_size * desired_bid
        if cost > max_usdc:
            quote_size = int(max_usdc / desired_bid)
            if quote_size < 5:  # Min order size
                return None
        
        # Check if we already have an entry order
        if token_id in self.entry_orders:
            existing = self.entry_orders[token_id]
            # Reprice if needed
            if abs(existing["price"] - desired_bid) >= tick:
                try:
                    self.clob.cancel_order(existing["order_id"])
                except:
                    pass
                del self.entry_orders[token_id]
            else:
                return existing["order_id"]
        
        # Place entry
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
                if result.error and "not enough balance" not in str(result.error):
                    print(f"[ENTRY] {label} failed: {result.error}", flush=True)
        
        except Exception as e:
            self.metrics.api_errors += 1
            print(f"[ENTRY] Error: {e}", flush=True)
        
        return None
    
    def _reconcile_tick(self) -> Dict:
        """Periodic reconciliation"""
        now = time.time()
        
        if now - self.last_reconcile < self.reconcile_interval_s:
            return {}
        
        self.last_reconcile = now
        self.metrics.reconcile_count += 1
        
        mismatches = self.position_manager.reconcile_from_rest()
        
        if mismatches:
            self.metrics.reconcile_mismatches += len(mismatches)
            for token, data in mismatches.items():
                print(f"[RECONCILE] {token[:20]}... {data}", flush=True)
                
                # Critical: REST shows inv but we thought 0
                if data["diff"] > 0:
                    print(f"[RECONCILE] CRITICAL: Missed fill detected!", flush=True)
        
        return mismatches
    
    def _log_tick(self, yes_book: dict, no_book: dict):
        """Log current state"""
        yes_shares = self.position_manager.get_shares(self.yes_token)
        no_shares = self.position_manager.get_shares(self.no_token)
        
        entry_count = len(self.entry_orders)
        exit_count = self.exit_supervisor.active_exit_count
        
        # Update MTM
        self.position_manager.update_mtm(self.yes_token, yes_book["best_bid"], yes_book["best_ask"])
        self.position_manager.update_mtm(self.no_token, no_book["best_bid"], no_book["best_ask"])
        
        mtm = self.position_manager.get_total_mtm()
        mae = self.position_manager.get_max_adverse_excursion()
        inv_age = self.position_manager.get_inventory_age_seconds()
        secs_left = self.seconds_to_settlement
        
        # Track max
        self.metrics.max_adverse_excursion = max(self.metrics.max_adverse_excursion, mae)
        self.metrics.max_inventory_age_s = max(self.metrics.max_inventory_age_s, inv_age)
        
        # Console log every 1s (every 4 ticks)
        if self.metrics.ticks % 4 == 0:
            yes_mid = (yes_book["best_bid"] + yes_book["best_ask"]) / 2
            no_mid = (no_book["best_bid"] + no_book["best_ask"]) / 2
            
            state_str = self.state.value.upper()
            inv_str = f"YES:{yes_shares:.1f} NO:{no_shares:.1f}"
            order_str = f"E:{entry_count} X:{exit_count}"
            time_str = f"{secs_left//60}:{secs_left%60:02d}"
            
            print(f"[{state_str}] {time_str} left | YES={yes_mid:.2f} NO={no_mid:.2f} | {inv_str} | {order_str} | MTM=${mtm:.2f}", flush=True)
        
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
                "entry_orders": entry_count,
                "exit_orders": exit_count,
                "mtm": mtm,
                "mae": mae,
                "inv_age": inv_age,
                "entries_blocked_late": self.metrics.entries_blocked_late,
                "entries_blocked_extreme": self.metrics.entries_blocked_extreme,
                "emergency_flatten_triggered": self.metrics.emergency_flatten_triggered
            }
            self.log_file.write(json.dumps(log_entry) + "\n")
            self.log_file.flush()
    
    def _run_tick(self):
        """Main loop tick (250ms)"""
        self.metrics.ticks += 1
        
        # Get books
        yes_book = self._get_book(self.yes_token)
        no_book = self._get_book(self.no_token)
        
        # Validate books
        if not yes_book["has_liquidity"] or not no_book["has_liquidity"]:
            if self.metrics.ticks % 4 == 0:
                print("[TICK] No valid book, skipping", flush=True)
            return
        
        # Calculate mids
        yes_mid = (yes_book["best_bid"] + yes_book["best_ask"]) / 2
        no_mid = (no_book["best_bid"] + no_book["best_ask"]) / 2
        
        # Poll fill tracker for accurate fill counting
        market_tokens = {self.yes_token, self.no_token}
        new_fills = self.fill_tracker.poll_fills(market_tokens)
        for fill in new_fills:
            # Notify exit supervisor
            self.exit_supervisor.on_fill(fill.token_id, fill.side, fill.size)
        
        # Reconcile periodically
        self._reconcile_tick()
        
        # Reset emergency flag if inventory is 0
        total_shares = self.position_manager.get_total_shares()
        if total_shares < 0.01 and self._emergency_flatten_triggered:
            self._emergency_flatten_triggered = False
            # Clear exit supervisor state
            self.exit_supervisor.clear_token_state(self.yes_token)
            self.exit_supervisor.clear_token_state(self.no_token)
        
        # EXIT SUPERVISOR - ALWAYS RUNS (Priority 1)
        # This is the key safety feature - exits are NEVER blocked
        self.exit_supervisor.tick(yes_book, no_book, self.yes_token, self.no_token)
        
        # Check flatten deadline (ENDGAME RULE 3)
        if self._check_flatten_deadline():
            if self.state != BotState.EXIT_ONLY:
                print(f"[EXIT_ONLY_MODE] Flatten deadline reached ({self.seconds_to_settlement}s < {FLATTEN_DEADLINE_SECS}s)", flush=True)
                self._cancel_entry_orders()
                self.state = BotState.EXIT_ONLY
        
        # Check inventory age emergency (ENDGAME RULE 5)
        if self._check_inventory_age_emergency():
            if self.state != BotState.EMERGENCY:
                print(f"[EMERGENCY_FLATTEN_TRIGGERED] Inventory age exceeded", flush=True)
                self._cancel_entry_orders()
                self.state = BotState.EMERGENCY
                # Set exit supervisor to emergency mode
                for token in [self.yes_token, self.no_token]:
                    exit_order = self.exit_supervisor.get_exit_order(token)
                    if exit_order:
                        exit_order.mode = ExitMode.EMERGENCY
        
        # Check spike
        in_spike = self._check_spike(yes_book, no_book)
        
        # Check endgame rules for entry blocking
        block_entries, block_reason = self._check_endgame_rules(yes_mid, no_mid)
        
        # State machine
        if self.state == BotState.FLATTEN:
            # Only exits, no new entries (startup mode)
            if self.position_manager.get_total_shares() < 0.01:
                secs_left = self.seconds_to_settlement
                if secs_left >= ENTRY_CUTOFF_SECS:
                    print("[FLATTEN] Complete, switching to QUOTING", flush=True)
                    self.state = BotState.QUOTING
                else:
                    print("[FLATTEN] Complete, but past entry cutoff -> EXIT_ONLY", flush=True)
                    self.state = BotState.EXIT_ONLY
        
        elif self.state == BotState.EXIT_ONLY:
            # Near settlement or extreme odds, only exits
            self._cancel_entry_orders()
            # Check if we can exit this mode (only if time permits and no inventory)
            if self.seconds_to_settlement >= ENTRY_CUTOFF_SECS:
                if self.position_manager.get_total_shares() < 0.01:
                    print("[EXIT_ONLY] Time permits and no inventory -> QUOTING", flush=True)
                    self.state = BotState.QUOTING
        
        elif self.state == BotState.EMERGENCY:
            # Emergency flatten - keep trying until flat
            self._cancel_entry_orders()
            if self.position_manager.get_total_shares() < 0.01:
                print("[EMERGENCY] Flatten complete -> EXIT_ONLY", flush=True)
                self.state = BotState.EXIT_ONLY
        
        elif self.state == BotState.QUOTING:
            if in_spike:
                # Spike pause - cancel entries, exits continue (ENDGAME RULE 4)
                self._cancel_entry_orders()
            elif block_entries:
                # Endgame rules block entries
                self._cancel_entry_orders()
            else:
                # Normal quoting
                self._place_entry_bid(self.yes_token, yes_book, "YES", "")
                self._place_entry_bid(self.no_token, no_book, "NO", "")
        
        # Log
        self._log_tick(yes_book, no_book)
    
    def run(self, duration_seconds: float, output_dir: str = "mm_out"):
        """Run the bot for specified duration"""
        
        # Safety check
        if self.live:
            if not os.environ.get("MM_EXIT_ENFORCED"):
                print("[SAFETY] LIVE mode requires MM_EXIT_ENFORCED=1", flush=True)
                return
        
        # Lock
        if not self._acquire_lock():
            return
        
        # Output
        out_path = Path(output_dir)
        out_path.mkdir(exist_ok=True)
        self.log_file = open(out_path / "run.jsonl", "w")
        
        # Signal handler
        def on_signal(sig, frame):
            print("\n[SHUTDOWN] Signal received", flush=True)
            self.running = False
            self.shutdown_event.set()
        
        signal.signal(signal.SIGINT, on_signal)
        signal.signal(signal.SIGTERM, on_signal)
        
        try:
            print(f"[START] Mode={'LIVE' if self.live else 'DRYRUN'} Duration={duration_seconds}s", flush=True)
            print(f"[START] Market ends in {self.seconds_to_settlement}s", flush=True)
            print(f"[START] Entry cutoff at {ENTRY_CUTOFF_SECS}s, Flatten at {FLATTEN_DEADLINE_SECS}s", flush=True)
            print(f"[START] Extreme odds cutoff: <{EXTREME_ODDS_LOW} or >{EXTREME_ODDS_HIGH}", flush=True)
            
            # Record session start cash for PnL tracking
            try:
                bal = self.clob.get_balance()
                self.fill_tracker.set_session_start_cash(bal.get('usdc', 0))
                print(f"[START] Session start cash: ${bal.get('usdc', 0):.2f}", flush=True)
            except:
                pass
            
            # Initial state
            self.state = self._check_startup_state()
            
            self.running = True
            start_time = time.time()
            
            while self.running:
                tick_start = time.time()
                
                # Check duration
                if tick_start - start_time >= duration_seconds:
                    print("[SHUTDOWN] Duration reached", flush=True)
                    break
                
                # Check if market ended
                if self.seconds_to_settlement <= 0:
                    print("[SHUTDOWN] Market ended", flush=True)
                    break
                
                # Run tick
                try:
                    self._run_tick()
                except Exception as e:
                    print(f"[ERROR] Tick error: {e}", flush=True)
                    self.metrics.api_errors += 1
                
                # Sleep to maintain 250ms tick
                elapsed = time.time() - tick_start
                sleep_time = max(0, (self.loop_interval_ms / 1000.0) - elapsed)
                if sleep_time > 0:
                    self.shutdown_event.wait(sleep_time)
        
        finally:
            print("[SHUTDOWN] Cleaning up...", flush=True)
            
            # Cancel entry orders
            self._cancel_entry_orders()
            
            # Final reconcile
            self.position_manager.reconcile_from_rest()
            
            # Report
            self._write_report(out_path)
            
            # Cleanup
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
            "endgame_rules": {
                "entry_cutoff_secs": ENTRY_CUTOFF_SECS,
                "flatten_deadline_secs": FLATTEN_DEADLINE_SECS,
                "extreme_odds_high": EXTREME_ODDS_HIGH,
                "extreme_odds_low": EXTREME_ODDS_LOW,
                "max_inventory_age_secs": MAX_INVENTORY_AGE_SECS
            },
            "metrics": {
                "ticks": self.metrics.ticks,
                "entries_posted": self.metrics.entries_posted,
                "entries_blocked_late": self.metrics.entries_blocked_late,
                "entries_blocked_extreme": self.metrics.entries_blocked_extreme,
                "entries_filled": fill_metrics["entry_fills"],
                "exits_posted": exit_metrics["exits_placed"],
                "exits_repriced": exit_metrics["exits_repriced"],
                "exits_filled": fill_metrics["exit_fills"],
                "emergency_exits": exit_metrics["emergency_exits"],
                "emergency_flatten_triggered": self.metrics.emergency_flatten_triggered,
                "reconcile_count": self.metrics.reconcile_count,
                "reconcile_mismatches": self.metrics.reconcile_mismatches,
                "spike_pauses": self.metrics.spike_pauses,
                "api_errors": self.metrics.api_errors,
                "max_adverse_excursion": self.metrics.max_adverse_excursion,
                "max_inventory_age_s": self.metrics.max_inventory_age_s,
                "balance_errors": exit_metrics["balance_errors"]
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
        print("FINAL REPORT", flush=True)
        print("=" * 60, flush=True)
        print(f"Ticks: {self.metrics.ticks}", flush=True)
        print(f"Entries posted: {self.metrics.entries_posted}", flush=True)
        print(f"Entries blocked (late): {self.metrics.entries_blocked_late}", flush=True)
        print(f"Entries blocked (extreme): {self.metrics.entries_blocked_extreme}", flush=True)
        print("-" * 40, flush=True)
        print(f"Entry fills (REST): {fill_metrics['entry_fills']}", flush=True)
        print(f"Exit fills (REST): {fill_metrics['exit_fills']}", flush=True)
        print(f"Complete round trips: {fill_metrics['complete_round_trips']}", flush=True)
        print("-" * 40, flush=True)
        print(f"Exits placed: {exit_metrics['exits_placed']}", flush=True)
        print(f"Exits repriced: {exit_metrics['exits_repriced']}", flush=True)
        print(f"Emergency exits: {exit_metrics['emergency_exits']}", flush=True)
        print(f"Balance errors: {exit_metrics['balance_errors']}", flush=True)
        print("-" * 40, flush=True)
        print(f"Emergency flatten triggered: {self.metrics.emergency_flatten_triggered}", flush=True)
        print(f"Reconcile mismatches: {self.metrics.reconcile_mismatches}", flush=True)
        print(f"Max adverse excursion: {self.metrics.max_adverse_excursion*100:.2f}c", flush=True)
        print(f"Max inventory age: {self.metrics.max_inventory_age_s:.1f}s", flush=True)
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
    
    # Load config
    config = Config.from_env("pm_api_config.json")
    
    if os.environ.get("LIVE") == "1":
        config.mode = RunMode.LIVE
    
    # Get market tokens
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
    
    # Run
    runner = SafeRunner(config, market.yes_token_id, market.no_token_id, market.end_time)
    runner.run(args.seconds, args.outdir)


if __name__ == "__main__":
    main()
