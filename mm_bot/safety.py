"""
Safety Module
=============
Kill switches, lock files, and safety checks.
"""

import os
import sys
import time
import json
import atexit
from pathlib import Path
from typing import Optional, Dict
from dataclasses import dataclass, field


@dataclass
class SafetyState:
    """Track safety conditions"""
    # Inventory without exit tracking
    inv_without_exit: Dict[str, float] = field(default_factory=dict)  # token -> timestamp
    inv_without_exit_threshold: float = 10.0  # seconds
    
    # Reconciliation mismatch tracking
    reconcile_mismatch_count: int = 0
    reconcile_mismatch_threshold: int = 2
    
    # Kill switch state
    kill_triggered: bool = False
    kill_reason: str = ""


class LockFile:
    """Prevent duplicate runners"""
    
    def __init__(self, path: str = "mm_bot.lock"):
        self.path = Path(path)
        self.pid = os.getpid()
        self._locked = False
    
    def acquire(self) -> bool:
        """Acquire lock. Returns False if another instance is running."""
        if self.path.exists():
            try:
                with open(self.path) as f:
                    data = json.load(f)
                old_pid = data.get("pid")
                
                # Check if old process is still alive
                if old_pid and self._is_process_alive(old_pid):
                    return False  # Another instance running
            except:
                pass  # Lock file corrupt, overwrite it
        
        # Write our lock
        with open(self.path, "w") as f:
            json.dump({"pid": self.pid, "started": time.time()}, f)
        
        self._locked = True
        atexit.register(self.release)
        return True
    
    def release(self):
        """Release lock"""
        if self._locked and self.path.exists():
            try:
                self.path.unlink()
            except:
                pass
        self._locked = False
    
    def _is_process_alive(self, pid: int) -> bool:
        """Check if process is alive"""
        try:
            if sys.platform == 'win32':
                import ctypes
                kernel32 = ctypes.windll.kernel32
                handle = kernel32.OpenProcess(0x1000, False, pid)  # PROCESS_QUERY_LIMITED_INFORMATION
                if handle:
                    kernel32.CloseHandle(handle)
                    return True
                return False
            else:
                os.kill(pid, 0)
                return True
        except:
            return False


class SafetyManager:
    """
    Manages all safety checks and kill switches.
    """
    
    def __init__(self, verbose: bool = True):
        self.state = SafetyState()
        self.lock = LockFile()
        self.verbose = verbose
        self._heartbeat_file = Path("mm_bot_heartbeat.json")
        self._last_heartbeat = 0.0
    
    def check_startup_requirements(self, config) -> tuple[bool, str]:
        """
        Check if bot is safe to start.
        Returns (can_start, reason)
        """
        # Check for exit enforcement flag in LIVE mode
        from .config import RunMode
        
        if config.mode == RunMode.LIVE:
            if not os.environ.get("MM_EXIT_ENFORCED") == "1":
                return False, "LIVE mode requires MM_EXIT_ENFORCED=1"
        
        # Check for duplicate runner
        if not self.lock.acquire():
            return False, "Another MM bot instance is already running"
        
        return True, ""
    
    def check_startup_inventory(self, clob, market) -> tuple[bool, dict]:
        """
        Check if we have existing inventory at startup.
        Returns (has_inventory, positions_dict)
        """
        positions = {}
        
        try:
            # Get open orders
            orders = clob.get_open_orders()
            
            # Check for any filled amounts
            for o in orders:
                if o.size_matched > 0:
                    positions[o.token_id] = {
                        "shares": o.size_matched,
                        "side": o.side,
                        "from": "open_order_partial_fill"
                    }
            
            # Get positions from API
            import requests
            r = requests.get(
                "https://data-api.polymarket.com/positions",
                params={"user": clob.config.api.proxy_address},
                timeout=10
            )
            if r.status_code == 200:
                for p in r.json():
                    token_id = p.get("asset", "")
                    size = float(p.get("size", 0))
                    if size > 0 and market:
                        # Only track positions for our market
                        if token_id in [market.yes_token_id, market.no_token_id]:
                            positions[token_id] = {
                                "shares": size,
                                "avg_price": float(p.get("avgPrice", 0)),
                                "from": "positions_api"
                            }
        except Exception as e:
            if self.verbose:
                print(f"[SAFETY] Error checking startup inventory: {e}", flush=True)
        
        return len(positions) > 0, positions
    
    def update_inv_exit_tracking(self, token_id: str, has_inv: bool, has_exit: bool):
        """
        Track inventory without corresponding exit order.
        """
        now = time.time()
        
        if has_inv and not has_exit:
            # Start tracking if not already
            if token_id not in self.state.inv_without_exit:
                self.state.inv_without_exit[token_id] = now
                if self.verbose:
                    print(f"[SAFETY] WARN: Inventory {token_id[:20]}... without exit order", flush=True)
        else:
            # Clear tracking
            if token_id in self.state.inv_without_exit:
                del self.state.inv_without_exit[token_id]
    
    def check_kill_conditions(self) -> tuple[bool, str]:
        """
        Check if kill switch should trigger.
        Returns (should_kill, reason)
        """
        now = time.time()
        
        # Check inventory without exit timeout
        for token_id, start_time in self.state.inv_without_exit.items():
            elapsed = now - start_time
            if elapsed > self.state.inv_without_exit_threshold:
                return True, f"Inventory {token_id[:20]}... without exit for {elapsed:.0f}s"
        
        # Check reconciliation mismatch
        if self.state.reconcile_mismatch_count >= self.state.reconcile_mismatch_threshold:
            return True, f"Reconciliation mismatch for {self.state.reconcile_mismatch_count} cycles"
        
        return False, ""
    
    def trigger_kill(self, reason: str):
        """Trigger kill switch"""
        self.state.kill_triggered = True
        self.state.kill_reason = reason
        print(f"[SAFETY] KILL SWITCH TRIGGERED: {reason}", flush=True)
    
    def is_killed(self) -> bool:
        """Check if kill switch has been triggered"""
        return self.state.kill_triggered
    
    def write_heartbeat(self):
        """Write heartbeat to file"""
        now = time.time()
        if now - self._last_heartbeat >= 1.0:
            try:
                with open(self._heartbeat_file, "w") as f:
                    json.dump({
                        "pid": os.getpid(),
                        "ts": now,
                        "killed": self.state.kill_triggered
                    }, f)
                self._last_heartbeat = now
            except:
                pass
    
    def cleanup(self):
        """Cleanup on exit"""
        self.lock.release()
        if self._heartbeat_file.exists():
            try:
                self._heartbeat_file.unlink()
            except:
                pass

