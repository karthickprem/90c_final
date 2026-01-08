"""
Runner V7 - REBATE FARMING STRATEGY
====================================

GOAL: Print money by earning maker rebates, not from directional bets.

STRATEGY:
1. Get filled as MAKER (post limit orders)
2. Exit quickly at breakeven or small profit
3. Collect rebate on every fill
4. Never hold to settlement

TWO MODES:
- MIDDLE ZONE (0.30-0.70): Higher rebates, moderate risk
- EXTREME ZONE (>0.90): Follow the trend, lower risk

EXIT LADDER (time-based):
- 0-10s:  Exit at entry + 2c (target profit)
- 10-20s: Exit at entry + 1c
- 20-30s: Exit at entry (scratch)
- 30-40s: Exit at entry - 1c (accept small loss)
- 40s+:   Market exit (taker if needed)

STOP LOSS: -3c from entry (always)
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
from typing import Optional, Dict, Set
from dataclasses import dataclass
from collections import deque

from mm_bot.config import Config, RunMode
from mm_bot.clob import ClobWrapper


class BotMode(Enum):
    STARTUP = "startup"
    MIDDLE_ZONE = "middle"      # 0.30 - 0.70: Both sides eligible
    EXTREME_ZONE = "extreme"    # >0.90 one side: Only dominant side
    EXIT_ONLY = "exit_only"     # Have position, managing exit
    SHUTDOWN = "shutdown"


# CONFIGURATION
MIN_SHARES = 5.0
SHARE_STEP = 0.01

# Zone boundaries
MIDDLE_LOW = 0.30
MIDDLE_HIGH = 0.70
EXTREME_THRESHOLD = 0.90

# Exit ladder (seconds after fill)
EXIT_TP_SECS = 10       # First 10s: Try for +2c
EXIT_REDUCE_SECS = 20   # 10-20s: Try for +1c
EXIT_SCRATCH_SECS = 30  # 20-30s: Scratch at entry
EXIT_LOSS_SECS = 40     # 30-40s: Accept -1c
EXIT_FORCE_SECS = 50    # 40s+: Market exit

# Risk limits
STOP_LOSS_CENTS = 3     # Exit if price drops 3c from entry
TAKE_PROFIT_CENTS = 2   # Target profit

# Timing
ENTRY_COOLDOWN_SECS = 3   # Wait between entries (fast for rebate farming)
OPENING_AGGRESSIVE_SECS = 90  # First 90s: Be very aggressive
LOOP_INTERVAL_MS = 100    # 100ms ticks for fast response


@dataclass
class Position:
    """Tracks a single position"""
    token_id: str
    side: str  # "YES" or "NO"
    shares: float
    entry_price: float
    entry_time: float
    
    exit_order_id: Optional[str] = None
    exit_price: float = 0.0
    
    @property
    def age_secs(self) -> float:
        return time.time() - self.entry_time
    
    def target_exit_price(self) -> float:
        """Get target exit price based on time since entry"""
        age = self.age_secs
        
        if age < EXIT_TP_SECS:
            # Try for +2c profit
            return min(0.99, self.entry_price + 0.02)
        elif age < EXIT_REDUCE_SECS:
            # Try for +1c profit
            return min(0.99, self.entry_price + 0.01)
        elif age < EXIT_SCRATCH_SECS:
            # Scratch at entry
            return self.entry_price
        elif age < EXIT_LOSS_SECS:
            # Accept -1c loss
            return max(0.01, self.entry_price - 0.01)
        else:
            # Force exit - aggressive price
            return max(0.01, self.entry_price - 0.02)
    
    def stop_loss_price(self) -> float:
        """Price at which we cut losses"""
        return max(0.01, self.entry_price - STOP_LOSS_CENTS / 100.0)


@dataclass
class Metrics:
    """Session metrics"""
    ticks: int = 0
    entries_posted: int = 0
    entries_filled: int = 0
    exits_posted: int = 0
    exits_filled: int = 0
    stop_losses: int = 0
    scratches: int = 0
    take_profits: int = 0
    
    total_rebate_eligible_fills: int = 0  # Maker fills
    total_pnl_cents: float = 0.0
    
    api_errors: int = 0


class RebateFarmingBot:
    """
    Rebate-focused market making bot.
    
    Key invariants:
    1. Only ONE position at a time
    2. Always exit quickly (never hold to settlement)
    3. Tight stops (3c max loss)
    4. Target small profits (1-2c) + rebates
    """
    
    def __init__(self, config: Config, yes_token: str, no_token: str, market_end_time: int):
        self.config = config
        self.yes_token = yes_token
        self.no_token = no_token
        self.market_end_time = market_end_time
        
        # Window timing (15 min = 900s)
        self.window_start_time = market_end_time - 900
        
        self.live = config.mode == RunMode.LIVE
        self.clob = ClobWrapper(config)
        
        # State
        self.mode = BotMode.STARTUP
        self.metrics = Metrics()
        self.position: Optional[Position] = None
        
        # Entry tracking
        self.pending_entry_id: Optional[str] = None
        self.pending_entry_token: Optional[str] = None
        self.pending_entry_price: float = 0.0  # Track the price we posted at
        self.pending_entry_size: float = 0.0   # Track the size we posted
        self.last_entry_time: float = 0
        
        # Track existing positions at startup (to detect NEW fills)
        self.startup_yes_shares: float = 0.0
        self.startup_no_shares: float = 0.0
        
        # Price history for volatility
        self.mid_history: deque = deque(maxlen=50)
        
        # Shutdown
        self.running = False
        self.shutdown_event = threading.Event()
        
        # Output
        self.log_file = None
    
    @property
    def seconds_left(self) -> int:
        return max(0, self.market_end_time - int(time.time()))
    
    @property
    def seconds_since_start(self) -> float:
        return time.time() - self.window_start_time
    
    @property
    def in_opening_period(self) -> bool:
        """True if within first OPENING_AGGRESSIVE_SECS of window"""
        return self.seconds_since_start < OPENING_AGGRESSIVE_SECS
    
    def _get_book(self, token_id: str) -> dict:
        """Get order book for token"""
        try:
            book = self.clob.get_order_book(token_id)
            if book and book.best_bid > 0.01 and book.best_ask < 0.99:
                return {
                    "bid": book.best_bid,
                    "ask": book.best_ask,
                    "mid": (book.best_bid + book.best_ask) / 2,
                    "spread": book.best_ask - book.best_bid,
                    "valid": True
                }
        except Exception as e:
            self.metrics.api_errors += 1
        return {"bid": 0, "ask": 0, "mid": 0.5, "spread": 0, "valid": False}
    
    def _get_rest_position(self, token_id: str) -> float:
        """Get position size from REST API"""
        try:
            import requests
            r = requests.get(
                "https://data-api.polymarket.com/positions",
                params={"user": self.config.api.proxy_address},
                timeout=10
            )
            if r.status_code == 200:
                for p in r.json():
                    if p.get("asset") == token_id:
                        return float(p.get("size", 0))
        except:
            pass
        return 0.0
    
    def _determine_mode(self, yes_mid: float, no_mid: float) -> BotMode:
        """Determine current trading mode based on prices"""
        if self.position:
            return BotMode.EXIT_ONLY
        
        # Check for extreme zone
        if yes_mid >= EXTREME_THRESHOLD or no_mid >= EXTREME_THRESHOLD:
            return BotMode.EXTREME_ZONE
        
        # Check for middle zone
        if MIDDLE_LOW <= yes_mid <= MIDDLE_HIGH:
            return BotMode.MIDDLE_ZONE
        
        # Outside tradeable zones
        return BotMode.STARTUP
    
    def _can_enter(self) -> bool:
        """Check if we can place a new entry"""
        if self.position:
            return False
        if self.pending_entry_id:
            return False
        
        # Shorter cooldown during opening period (be aggressive!)
        cooldown = 2 if self.in_opening_period else ENTRY_COOLDOWN_SECS
        if time.time() - self.last_entry_time < cooldown:
            return False
        
        if self.seconds_left < 120:  # Don't enter in last 2 min
            return False
        return True
    
    def _try_entry_middle(self, yes_book: dict, no_book: dict) -> bool:
        """
        MIDDLE ZONE entry: Quote the side with better spread.
        During opening period: Be aggressive, skip spread check.
        """
        if not self._can_enter():
            return False
        
        # Choose side with wider spread (more opportunity)
        if yes_book["spread"] >= no_book["spread"]:
            token_id = self.yes_token
            book = yes_book
            label = "YES"
        else:
            token_id = self.no_token
            book = no_book
            label = "NO"
        
        # During opening period: Skip spread check (be aggressive!)
        if not self.in_opening_period and book["spread"] < 0.01:
            return False
        
        # Bid at best_bid (join queue)
        bid_price = book["bid"]
        if bid_price < 0.05:
            return False  # Price too low
        
        # Calculate affordable size (FIX: Don't fail if MIN_SHARES too expensive)
        max_cost = self.config.risk.max_usdc_locked
        max_affordable_shares = int(max_cost / bid_price)
        size = max(MIN_SHARES, max_affordable_shares)
        
        # If we can't afford MIN_SHARES, use what we can afford (min 5 for API)
        if max_affordable_shares < MIN_SHARES:
            size = MIN_SHARES  # Try anyway, might work
            cost = size * bid_price
            if cost > max_cost * 1.5:  # Allow 50% overage
                print(f"[SKIP] Can't afford {size} @ {bid_price:.2f} (${cost:.2f} > ${max_cost:.2f})", flush=True)
                return False
        
        if self.in_opening_period:
            print(f"[OPENING] Aggressive entry (spread={book['spread']*100:.1f}c)", flush=True)
        
        return self._place_entry(token_id, bid_price, size, label)
    
    def _try_entry_extreme(self, yes_book: dict, no_book: dict) -> bool:
        """
        EXTREME ZONE entry: Quote only the dominant side (>90%).
        This side is likely to win, so we follow the trend.
        """
        if not self._can_enter():
            return False
        
        # Find the dominant side
        if yes_book["mid"] >= EXTREME_THRESHOLD:
            token_id = self.yes_token
            book = yes_book
            label = "YES"
        elif no_book["mid"] >= EXTREME_THRESHOLD:
            token_id = self.no_token
            book = no_book
            label = "NO"
        else:
            return False
        
        # Bid at best_bid
        bid_price = book["bid"]
        size = MIN_SHARES
        
        cost = size * bid_price
        if cost > self.config.risk.max_usdc_locked:
            return False
        
        return self._place_entry(token_id, bid_price, size, label)
    
    def _place_entry(self, token_id: str, price: float, size: float, label: str) -> bool:
        """Place entry order"""
        if not self.live:
            print(f"[ENTRY] Would BID {label} {size} @ {price:.4f}", flush=True)
            return False
        
        try:
            result = self.clob.post_order(
                token_id=token_id,
                side="BUY",
                price=price,
                size=size,
                post_only=True  # MAKER only!
            )
            
            if result.success and result.order_id:
                self.pending_entry_id = result.order_id
                self.pending_entry_token = token_id
                self.pending_entry_price = price  # CRITICAL: Track our order price!
                self.pending_entry_size = size    # Track our order size!
                self.last_entry_time = time.time()
                self.metrics.entries_posted += 1
                print(f"[ENTRY] Posted {label} BID {size} @ {price:.4f}", flush=True)
                return True
            else:
                if result.error:
                    print(f"[ENTRY] Failed: {result.error}", flush=True)
        except Exception as e:
            self.metrics.api_errors += 1
            print(f"[ENTRY] Error: {e}", flush=True)
        
        return False
    
    def _check_entry_fill(self) -> bool:
        """Check if pending entry was filled"""
        if not self.pending_entry_id:
            return False
        
        # Check REST position
        current_shares = self._get_rest_position(self.pending_entry_token)
        
        # Get baseline (shares we had at startup)
        if self.pending_entry_token == self.yes_token:
            baseline = self.startup_yes_shares
        else:
            baseline = self.startup_no_shares
        
        # Only count NEW shares as a fill
        new_shares = current_shares - baseline
        
        if new_shares >= MIN_SHARES - 0.1:
            # FILLED! Create position
            label = "YES" if self.pending_entry_token == self.yes_token else "NO"
            
            # CRITICAL FIX: Use our ORDER PRICE, not current book bid!
            entry_price = self.pending_entry_price
            fill_shares = min(new_shares, self.pending_entry_size + 1)  # Allow small overfill
            
            self.position = Position(
                token_id=self.pending_entry_token,
                side=label,
                shares=fill_shares,
                entry_price=entry_price,
                entry_time=time.time()
            )
            
            self.metrics.entries_filled += 1
            self.metrics.total_rebate_eligible_fills += 1
            
            print(f"[FILL] {label} {fill_shares:.2f} @ {entry_price:.4f} (NEW shares, baseline={baseline:.2f}) -> EXIT_ONLY", flush=True)
            
            # Update baseline for future
            if self.pending_entry_token == self.yes_token:
                self.startup_yes_shares = current_shares
            else:
                self.startup_no_shares = current_shares
            
            # Clear pending
            self.pending_entry_id = None
            self.pending_entry_token = None
            self.pending_entry_price = 0
            self.pending_entry_size = 0
            
            return True
        
        return False
    
    def _manage_exit(self, book: dict):
        """
        Manage exit for current position.
        
        KEY FIX: If market moved in our favor (bid > entry+1c), TAKE PROFIT NOW!
        Don't wait for maker fill - just exit at bid (taker).
        """
        if not self.position:
            return
        
        pos = self.position
        current_bid = book["bid"]
        
        # Check stop loss first
        if current_bid <= pos.stop_loss_price():
            print(f"[STOP] Price {current_bid:.4f} hit stop {pos.stop_loss_price():.4f}", flush=True)
            self._force_exit(current_bid)
            self.metrics.stop_losses += 1
            return
        
        # KEY FIX: If we're in profit (bid > entry + 1c), TAKE IT NOW!
        profit_cents = (current_bid - pos.entry_price) * 100
        if profit_cents >= 1.0:
            print(f"[PROFIT] +{profit_cents:.1f}c available! Taking profit at {current_bid:.4f}", flush=True)
            self._force_exit(current_bid)  # Taker exit to guarantee fill
            self.metrics.take_profits += 1
            return
        
        # Otherwise, use time-based ladder for exit
        age = pos.age_secs
        
        if age < EXIT_TP_SECS:
            # Try for +2c as maker
            target_price = min(0.99, pos.entry_price + 0.02)
            phase = "TP+2c"
        elif age < EXIT_REDUCE_SECS:
            target_price = min(0.99, pos.entry_price + 0.01)
            phase = "TP+1c"
        elif age < EXIT_SCRATCH_SECS:
            target_price = pos.entry_price
            phase = "SCRATCH"
        elif age < EXIT_LOSS_SECS:
            target_price = max(0.01, pos.entry_price - 0.01)
            phase = "LOSS-1c"
        else:
            # Force exit after timeout
            print(f"[TIMEOUT] Forcing exit after {age:.0f}s", flush=True)
            self._force_exit(current_bid)
            return
        
        # Post/update exit order
        if not pos.exit_order_id:
            self._post_exit(target_price, phase)
        elif abs(pos.exit_price - target_price) >= 0.01:
            self._cancel_and_repost_exit(target_price, phase)
        
        # Check if exit was filled
        remaining = self._get_rest_position(pos.token_id)
        if remaining < 0.1:
            pnl_cents = (pos.exit_price - pos.entry_price) * 100 * pos.shares
            self.metrics.exits_filled += 1
            self.metrics.total_pnl_cents += pnl_cents
            
            if pnl_cents > 0.5:
                self.metrics.take_profits += 1
                result = "TP"
            elif pnl_cents < -0.5:
                result = "LOSS"
            else:
                self.metrics.scratches += 1
                result = "SCRATCH"
            
            print(f"[EXIT] {result} {pnl_cents:+.1f}c after {pos.age_secs:.1f}s", flush=True)
            self.position = None
    
    def _post_exit(self, price: float, phase: str):
        """Post exit order"""
        if not self.position or not self.live:
            return
        
        pos = self.position
        size = pos.shares
        
        # Floor to step
        size = int(size / SHARE_STEP) * SHARE_STEP
        if size < MIN_SHARES:
            return  # Dust
        
        try:
            result = self.clob.post_order(
                token_id=pos.token_id,
                side="SELL",
                price=price,
                size=size,
                post_only=True
            )
            
            if result.success and result.order_id:
                pos.exit_order_id = result.order_id
                pos.exit_price = price
                self.metrics.exits_posted += 1
                print(f"[EXIT] Posted ({phase}) SELL {size:.2f} @ {price:.4f}", flush=True)
        except Exception as e:
            self.metrics.api_errors += 1
    
    def _cancel_and_repost_exit(self, new_price: float, phase: str):
        """Cancel current exit and post new one"""
        if not self.position:
            return
        
        pos = self.position
        
        # Cancel
        if pos.exit_order_id:
            try:
                self.clob.cancel_order(pos.exit_order_id)
                time.sleep(0.3)
            except:
                pass
            pos.exit_order_id = None
        
        # Post new
        self._post_exit(new_price, phase)
    
    def _force_exit(self, price: float):
        """Force exit with taker order if needed"""
        if not self.position or not self.live:
            return
        
        pos = self.position
        
        # Cancel any pending exit
        if pos.exit_order_id:
            try:
                self.clob.cancel_order(pos.exit_order_id)
            except:
                pass
        
        # Post aggressive sell (taker)
        try:
            result = self.clob.post_order(
                token_id=pos.token_id,
                side="SELL",
                price=price,
                size=pos.shares,
                post_only=False  # Allow taker
            )
            
            if result.success:
                pnl_cents = (price - pos.entry_price) * 100 * pos.shares
                self.metrics.total_pnl_cents += pnl_cents
                print(f"[FORCE EXIT] SELL @ {price:.4f}, PnL={pnl_cents:+.1f}c", flush=True)
                self.position = None
        except Exception as e:
            self.metrics.api_errors += 1
    
    def _run_tick(self):
        """Main loop tick"""
        self.metrics.ticks += 1
        
        # Get books
        yes_book = self._get_book(self.yes_token)
        no_book = self._get_book(self.no_token)
        
        if not yes_book["valid"] or not no_book["valid"]:
            return
        
        # Update mid history
        self.mid_history.append(yes_book["mid"])
        
        # Check for pending entry fill
        if self.pending_entry_id:
            self._check_entry_fill()
        
        # Determine mode
        self.mode = self._determine_mode(yes_book["mid"], no_book["mid"])
        
        # Act based on mode
        if self.mode == BotMode.EXIT_ONLY:
            pos_book = self._get_book(self.position.token_id)
            self._manage_exit(pos_book)
        
        elif self.mode == BotMode.MIDDLE_ZONE:
            self._try_entry_middle(yes_book, no_book)
        
        elif self.mode == BotMode.EXTREME_ZONE:
            self._try_entry_extreme(yes_book, no_book)
        
        # Log every 4 ticks (~400ms)
        if self.metrics.ticks % 4 == 0:
            self._log_tick(yes_book, no_book)
    
    def _log_tick(self, yes_book: dict, no_book: dict):
        """Log current state"""
        time_str = f"{self.seconds_left//60}:{self.seconds_left%60:02d}"
        yes_mid = yes_book["mid"]
        no_mid = no_book["mid"]
        
        pos_str = ""
        if self.position:
            pos = self.position
            pos_str = f"{pos.side}:{pos.shares:.1f}@{pos.entry_price:.2f} age={pos.age_secs:.0f}s"
        
        mode_str = self.mode.value.upper()[:6]
        
        print(f"[{mode_str}] {time_str} | YES={yes_mid:.2f} NO={no_mid:.2f} | {pos_str}", flush=True)
    
    def _cleanup_on_start(self):
        """Cancel all orders and RECORD existing positions (to distinguish from new fills)"""
        print("[STARTUP] Cleaning up existing orders/positions...", flush=True)
        
        if not self.live:
            return
        
        # Cancel all open orders
        try:
            self.clob.cancel_all()
            print("[CLOB] KILLED ALL ORDERS", flush=True)
            time.sleep(1)
        except Exception as e:
            print(f"[STARTUP] Cancel orders error: {e}", flush=True)
        
        # CRITICAL: Record baseline positions BEFORE trying to close
        # This prevents confusing old positions with new fills!
        self.startup_yes_shares = self._get_rest_position(self.yes_token)
        self.startup_no_shares = self._get_rest_position(self.no_token)
        
        # Try to close existing positions
        for token_id, label in [(self.yes_token, "YES"), (self.no_token, "NO")]:
            shares = self.startup_yes_shares if token_id == self.yes_token else self.startup_no_shares
            if shares >= MIN_SHARES:
                print(f"[STARTUP] Found {label} position: {shares:.2f} shares, closing...", flush=True)
                book = self._get_book(token_id)
                if book["valid"] and book["bid"] > 0.01:
                    try:
                        result = self.clob.post_order(
                            token_id=token_id,
                            side="SELL",
                            price=book["bid"],
                            size=shares,
                            post_only=False  # Taker to ensure fill
                        )
                        if result.success:
                            print(f"[STARTUP] Closed {label} position", flush=True)
                            # Update baseline after closing
                            if token_id == self.yes_token:
                                self.startup_yes_shares = 0
                            else:
                                self.startup_no_shares = 0
                        else:
                            print(f"[STARTUP] Failed to close (will track as baseline): {result.error}", flush=True)
                    except Exception as e:
                        print(f"[STARTUP] Close error (will track as baseline): {e}", flush=True)
                    time.sleep(1)
            elif shares > 0:
                print(f"[STARTUP] {label} dust: {shares:.2f} (below min, baseline set)", flush=True)
        
        print(f"[STARTUP] Baseline: YES={self.startup_yes_shares:.2f}, NO={self.startup_no_shares:.2f}", flush=True)
    
    def _cleanup_on_shutdown(self):
        """Cancel all orders and close positions on shutdown"""
        print("[SHUTDOWN] Cleaning up...", flush=True)
        
        if not self.live:
            return
        
        # Cancel all open orders first
        try:
            self.clob.cancel_all()
            print("[SHUTDOWN] Cancelled all open orders", flush=True)
            time.sleep(1)
        except Exception as e:
            print(f"[SHUTDOWN] Cancel error: {e}", flush=True)
        
        # Close any positions
        for token_id, label in [(self.yes_token, "YES"), (self.no_token, "NO")]:
            shares = self._get_rest_position(token_id)
            if shares >= MIN_SHARES:
                print(f"[SHUTDOWN] Closing {label} position: {shares:.2f} shares", flush=True)
                book = self._get_book(token_id)
                if book["valid"] and book["bid"] > 0.01:
                    try:
                        result = self.clob.post_order(
                            token_id=token_id,
                            side="SELL",
                            price=book["bid"],
                            size=shares,
                            post_only=False
                        )
                        if result.success:
                            print(f"[SHUTDOWN] Closed {label} position @ {book['bid']:.4f}", flush=True)
                    except Exception as e:
                        print(f"[SHUTDOWN] Close error: {e}", flush=True)
                    time.sleep(1)
        
        print("[SHUTDOWN] Cleanup complete", flush=True)
    
    def run(self, duration_seconds: float, output_dir: str = "mm_out"):
        """Run the bot"""
        if self.live and not os.environ.get("MM_EXIT_ENFORCED"):
            print("[SAFETY] LIVE mode requires MM_EXIT_ENFORCED=1", flush=True)
            return
        
        out_path = Path(output_dir)
        out_path.mkdir(exist_ok=True)
        
        def on_signal(sig, frame):
            print("\n[SHUTDOWN] Signal received", flush=True)
            self.running = False
            self.shutdown_event.set()
        
        signal.signal(signal.SIGINT, on_signal)
        signal.signal(signal.SIGTERM, on_signal)
        
        try:
            print(f"[START] V7 REBATE FARMING BOT", flush=True)
            print(f"[START] Mode={'LIVE' if self.live else 'DRYRUN'}", flush=True)
            print(f"[START] Market ends in {self.seconds_left}s", flush=True)
            print(f"[START] MIDDLE ZONE: {MIDDLE_LOW}-{MIDDLE_HIGH}", flush=True)
            print(f"[START] EXTREME ZONE: >{EXTREME_THRESHOLD}", flush=True)
            print(f"[START] Stop Loss: {STOP_LOSS_CENTS}c, Take Profit: {TAKE_PROFIT_CENTS}c", flush=True)
            
            # SAFETY: Clean up before starting
            self._cleanup_on_start()
            
            self.running = True
            start_time = time.time()
            
            while self.running:
                tick_start = time.time()
                
                if tick_start - start_time >= duration_seconds:
                    print("[SHUTDOWN] Duration reached", flush=True)
                    break
                
                if self.seconds_left <= 0:
                    print("[SHUTDOWN] Market ended", flush=True)
                    break
                
                try:
                    self._run_tick()
                except Exception as e:
                    print(f"[ERROR] Tick error: {e}", flush=True)
                    self.metrics.api_errors += 1
                
                elapsed = time.time() - tick_start
                sleep_time = max(0, (LOOP_INTERVAL_MS / 1000.0) - elapsed)
                if sleep_time > 0:
                    self.shutdown_event.wait(sleep_time)
        
        finally:
            # SAFETY: Always clean up on exit
            self._cleanup_on_shutdown()
            self._print_report()
    
    def _print_report(self):
        """Print final report"""
        m = self.metrics
        
        print("\n" + "=" * 60, flush=True)
        print("V7 REBATE FARMING REPORT", flush=True)
        print("=" * 60, flush=True)
        print(f"Ticks: {m.ticks}", flush=True)
        print(f"Entries posted: {m.entries_posted}", flush=True)
        print(f"Entries filled: {m.entries_filled}", flush=True)
        print(f"Exits filled: {m.exits_filled}", flush=True)
        print("-" * 40, flush=True)
        print(f"Take profits: {m.take_profits}", flush=True)
        print(f"Scratches: {m.scratches}", flush=True)
        print(f"Stop losses: {m.stop_losses}", flush=True)
        print("-" * 40, flush=True)
        print(f"Trade PnL: {m.total_pnl_cents:+.1f}c", flush=True)
        print(f"Rebate-eligible fills: {m.total_rebate_eligible_fills}", flush=True)
        print(f"Est. rebates (0.5c/fill): {m.total_rebate_eligible_fills * 0.5:.1f}c", flush=True)
        print(f"NET (trade + rebate): {m.total_pnl_cents + m.total_rebate_eligible_fills * 0.5:+.1f}c", flush=True)
        print("=" * 60, flush=True)


def main():
    import argparse
    
    parser = argparse.ArgumentParser()
    parser.add_argument("--seconds", type=int, default=900)
    parser.add_argument("--outdir", default="mm_out_v7")
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
    print(f"[MARKET] Ends in: {market.time_str}", flush=True)
    
    bot = RebateFarmingBot(config, market.yes_token_id, market.no_token_id, market.end_time)
    bot.run(args.seconds, args.outdir)


if __name__ == "__main__":
    main()

