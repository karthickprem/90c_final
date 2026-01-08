"""
Main Runner V2 - SAFE VERSION
==============================
With proper:
- Kill switches
- Exit enforcement
- Logging and observability
- Lock file protection
"""

import sys
import time
import json
import signal
from datetime import datetime
from typing import Optional, Dict
from pathlib import Path

# Force unbuffered output
sys.stdout.reconfigure(line_buffering=True) if hasattr(sys.stdout, 'reconfigure') else None

from .config import Config, RunMode
from .clob import ClobWrapper, OrderBook
from .market import MarketResolver, MarketInfo
from .inventory import InventoryManager
from .quoting import QuoteEngine, Quote
from .order_manager import OrderManager, OrderRole
from .safety import SafetyManager
from .balance import BalanceManager, AccountSnapshot


class MMRunnerV2:
    """
    Market Making Bot Runner V2 - SAFE VERSION
    
    Key safety features:
    - Lock file prevents duplicate instances
    - Exit enforcement: inventory MUST have exit orders
    - Reconciliation every 10s
    - Aggressive exit repricing (3s/6s/flatten)
    - Kill switch on safety violations
    """
    
    def __init__(self, config: Config):
        self.config = config
        
        # Components
        self.clob = ClobWrapper(config)
        self.market_resolver = MarketResolver(config)
        self.inventory = InventoryManager(config)
        self.quote_engine = QuoteEngine(config)
        self.order_manager = OrderManager(config, self.clob)
        self.safety = SafetyManager(verbose=config.verbose)
        self.balance_mgr = BalanceManager(self.clob, config)
        
        # Account snapshot (updated each tick)
        self._current_snapshot: Optional[AccountSnapshot] = None
        
        # State
        self.running = False
        self.current_market: Optional[MarketInfo] = None
        self.last_window_slug: Optional[str] = None
        self.flatten_mode = False
        
        # Exit tracking
        self._exit_placed_time: Dict[str, float] = {}  # token_id -> timestamp
        
        # Reconciliation
        self._last_reconcile = 0.0
        self._reconcile_interval = 10.0
        
        # Spike detection
        self._last_mid: Dict[str, float] = {}
        self._last_mid_time: Dict[str, float] = {}
        self._cooldown_until: Dict[str, float] = {}
        self._spike_threshold = 0.02
        self._spike_window_secs = 5.0
        self._cooldown_secs = 10.0
        
        # Flatten threshold
        self._flatten_threshold_secs = 120
        
        # Metrics
        self.start_time = 0.0
        self.ticks = 0
        self.orders_placed = 0
        self.fills_received = 0
        self.exits_placed = 0
        self.reconciles = 0
        self.skipped_min_notional = 0
        self.flatten_triggered = False
        
        # Logging
        self.log_file = Path(config.log_file) if config.log_file else None
        
        # Signal handling
        signal.signal(signal.SIGINT, self._signal_handler)
        signal.signal(signal.SIGTERM, self._signal_handler)
    
    def _signal_handler(self, signum, frame):
        """Handle shutdown signals"""
        self._log("SHUTDOWN", "Shutdown signal received")
        self.stop()
    
    def _log(self, event: str, msg: str = "", data: dict = None, flush: bool = True):
        """Log event to console and JSONL file"""
        ts = datetime.now()
        ts_str = ts.strftime("%H:%M:%S.%f")[:-3]
        
        # Console output
        console_msg = f"[{ts_str}] [{event}] {msg}"
        print(console_msg, flush=flush)
        
        # JSONL output
        if self.log_file:
            entry = {
                "ts": ts.isoformat(),
                "event": event,
                "msg": msg
            }
            if data:
                entry["data"] = data
            with open(self.log_file, "a") as f:
                f.write(json.dumps(entry) + "\n")
    
    def _log_tick(self, window: dict, yes_book: OrderBook, no_book: OrderBook):
        """Log tick summary with Account Snapshot every second"""
        inv = self.inventory.get_summary()
        om = self.order_manager.get_metrics()
        
        # Update account snapshot
        self._current_snapshot = self.balance_mgr.get_snapshot()
        snap = self._current_snapshot
        
        data = {
            "secs_left": window.get("secs_left", 0),
            "yes_bid": yes_book.best_bid if yes_book else 0,
            "yes_ask": yes_book.best_ask if yes_book else 0,
            "no_bid": no_book.best_bid if no_book else 0,
            "no_ask": no_book.best_ask if no_book else 0,
            "yes_inv": inv["yes_shares"],
            "no_inv": inv["no_shares"],
            "open_orders": om["active_orders"],
            "flatten": self.flatten_mode,
            # Account Snapshot (CRITICAL for sizing)
            "account": snap.to_dict()
        }
        
        secs_left = window.get('secs_left', 0)
        
        # Log account snapshot
        self._log("ACCOUNT", snap.to_log_string(), snap.to_dict())
        
        # Log tick
        self._log("TICK", f"T-{secs_left//60}:{secs_left%60:02d} | YES:{inv['yes_shares']:.0f} NO:{inv['no_shares']:.0f} | Orders:{om['active_orders']} | Flatten:{self.flatten_mode}", data)
    
    def setup(self) -> bool:
        """
        Initialize components with safety checks.
        Returns False if unsafe to continue.
        """
        self._log("STARTUP", "=" * 50)
        self._log("STARTUP", "POLYMARKET MM BOT V2 - SAFE VERSION")
        self._log("STARTUP", "=" * 50)
        
        # Print config
        self._log("CONFIG", f"Mode: {self.config.mode.value.upper()}")
        self._log("CONFIG", f"Max USDC Locked: ${self.config.risk.max_usdc_locked:.2f}")
        self._log("CONFIG", f"Max Shares/Token: {self.config.risk.max_inv_shares_per_token}")
        self._log("CONFIG", f"Quote Size: {self.config.quoting.base_quote_size}")
        
        # Safety checks
        can_start, reason = self.safety.check_startup_requirements(self.config)
        if not can_start:
            self._log("SAFETY_FAIL", f"Cannot start: {reason}")
            return False
        
        # Validate config
        errors = self.config.validate()
        if errors:
            for err in errors:
                self._log("CONFIG_ERROR", err)
            if self.config.mode == RunMode.LIVE:
                return False
        
        # Resolve market
        self.current_market = self.market_resolver.resolve_market()
        if not self.current_market:
            self._log("MARKET_ERROR", "Could not resolve market tokens")
            return False
        
        self._log("MARKET", f"Slug: {self.current_market.slug}")
        self._log("MARKET", f"YES token: {self.current_market.yes_token_id[:30]}...")
        self._log("MARKET", f"NO token: {self.current_market.no_token_id[:30]}...")
        
        # Set up inventory
        self.inventory.set_tokens(
            self.current_market.yes_token_id,
            self.current_market.no_token_id
        )
        
        # Get initial account snapshot
        if self.config.mode != RunMode.DRYRUN:
            self._current_snapshot = self.balance_mgr.get_snapshot()
            snap = self._current_snapshot
            
            self._log("ACCOUNT", "=" * 40)
            self._log("ACCOUNT", "INITIAL ACCOUNT SNAPSHOT")
            self._log("ACCOUNT", "=" * 40)
            self._log("ACCOUNT", f"Cash (spendable): ${snap.cash_available_usdc:.2f}")
            self._log("ACCOUNT", f"Locked in buys:   ${snap.locked_usdc_in_open_buys:.2f}")
            self._log("ACCOUNT", f"Positions MTM:    ${snap.positions_mtm_usdc:.2f}")
            self._log("ACCOUNT", f"Equity estimate:  ${snap.equity_estimate_usdc:.2f}")
            self._log("ACCOUNT", f"Spendable:        ${snap.spendable_usdc:.2f}")
            self._log("ACCOUNT", f"Safety buffer:    ${snap.safety_buffer:.2f}")
            self._log("ACCOUNT", "=" * 40)
            
            # CRITICAL: If positions exist at startup, go FLATTEN first
            if snap.positions_mtm_usdc > 0.01:
                self._log("STARTUP_INV", f"Found existing positions: ${snap.positions_mtm_usdc:.2f} MTM")
                self.flatten_mode = True
                self.flatten_triggered = True
                self._log("FLATTEN_MODE", "Entering FLATTEN mode - will close positions before trading")
                self._log("FLATTEN_MODE", "Bot will NOT place new entry orders until positions are closed")
            
            self.inventory.reconcile(
                usdc_balance=snap.cash_available_usdc,
                position_value=snap.positions_mtm_usdc
            )
        
        # Check for existing inventory in our specific market tokens
        has_inv, positions = self.safety.check_startup_inventory(self.clob, self.current_market)
        if has_inv:
            self._log("STARTUP_INV", f"BTC 15m inventory: {positions}")
            if not self.flatten_mode:
                self.flatten_mode = True
                self.flatten_triggered = True
                self._log("FLATTEN_MODE", "Entering FLATTEN mode for BTC 15m positions")
        
        self._log("STARTUP", "Setup complete")
        return True
    
    def run(self, duration_seconds: float = 60.0):
        """Main run loop"""
        if not self.setup():
            self._log("ABORT", "Setup failed, aborting")
            return
        
        self.running = True
        self.start_time = time.time()
        deadline = self.start_time + duration_seconds
        
        self._log("RUN", f"Starting main loop for {duration_seconds:.0f}s")
        
        try:
            while self.running and time.time() < deadline:
                tick_start = time.time()
                self.ticks += 1
                
                # Write heartbeat
                self.safety.write_heartbeat()
                
                # Check kill switch
                if self.safety.is_killed():
                    self._log("KILLED", f"Kill switch active: {self.safety.state.kill_reason}")
                    self.order_manager.cancel_all()
                    break
                
                # Check window change
                window = self.market_resolver.get_current_window()
                if window["slug"] != self.last_window_slug:
                    self._handle_window_change(window)
                    self.last_window_slug = window["slug"]
                
                # Get order books
                yes_book = self.clob.get_order_book(self.current_market.yes_token_id) if self.current_market else None
                no_book = self.clob.get_order_book(self.current_market.no_token_id) if self.current_market else None
                
                # Sanity check: books must have real values
                if yes_book and (yes_book.best_bid <= 0 or yes_book.best_ask >= 1):
                    self._log("BOOK_WARN", f"YES book empty/invalid: bid={yes_book.best_bid} ask={yes_book.best_ask}")
                if no_book and (no_book.best_bid <= 0 or no_book.best_ask >= 1):
                    self._log("BOOK_WARN", f"NO book empty/invalid: bid={no_book.best_bid} ask={no_book.best_ask}")
                
                # Reconciliation (every 10s)
                self._reconcile()
                
                # EXIT ENFORCEMENT (ALWAYS runs, even in cooldown/flatten)
                self._enforce_exits(yes_book, no_book)
                
                # Check flatten condition (near end of window)
                if window.get("secs_left", 900) < self._flatten_threshold_secs:
                    if not self.flatten_mode:
                        self._log("FLATTEN_MODE", "Entering flatten mode (near settlement)")
                        self.flatten_mode = True
                
                # Entry quotes (skip if flattening or killed)
                if not self.flatten_mode and not self.safety.is_killed():
                    self._entry_cycle(yes_book, no_book)
                
                # Check safety conditions
                self._check_safety()
                
                # Log tick (every second)
                if self.ticks % 1 == 0:
                    self._log_tick(window, yes_book, no_book)
                
                # Sleep to target 1s interval
                elapsed = time.time() - tick_start
                sleep_time = max(0, 1.0 - elapsed)
                if sleep_time > 0:
                    time.sleep(sleep_time)
        
        except Exception as e:
            self._log("ERROR", f"Main loop error: {e}")
            import traceback
            traceback.print_exc()
        
        finally:
            self.stop()
    
    def _handle_window_change(self, window: dict):
        """Handle 15-min window transition"""
        self._log("WINDOW", f"New window: {window['slug']}")
        
        # Cancel all entry orders
        self.order_manager.cancel_all()
        
        # Resolve new market
        self.current_market = self.market_resolver.resolve_market(window["slug"])
        
        if self.current_market:
            self.inventory.set_tokens(
                self.current_market.yes_token_id,
                self.current_market.no_token_id
            )
        
        # Reset flatten mode for new window (unless we have inventory)
        if self.inventory.get_yes_shares() > 0 or self.inventory.get_no_shares() > 0:
            self.flatten_mode = True
        else:
            self.flatten_mode = False
    
    def _reconcile(self):
        """Periodic reconciliation with REST API"""
        now = time.time()
        if now - self._last_reconcile < self._reconcile_interval:
            return
        
        self._last_reconcile = now
        self.reconciles += 1
        
        try:
            # Get actual balance
            balance = self.clob.get_balance()
            
            # Get internal state
            internal_yes = self.inventory.get_yes_shares()
            internal_no = self.inventory.get_no_shares()
            
            # Log reconciliation
            self._log("RECONCILE", f"USDC: ${balance['usdc']:.2f} | Positions: ${balance['positions']:.2f} | Internal YES:{internal_yes:.1f} NO:{internal_no:.1f}")
            
            # Update internal state
            self.inventory.reconcile(
                usdc_balance=balance["usdc"],
                position_value=balance["positions"]
            )
            
            # Sync orders with API
            api_orders = self.clob.get_open_orders()
            self.order_manager.sync_with_api(api_orders)
            
            # Check for fills
            for order in api_orders:
                if order.size_matched > 0:
                    self._log("FILL_RECEIVED", f"{order.side} {order.size_matched:.1f} @ {order.price:.2f}", {
                        "order_id": order.order_id[:30],
                        "side": order.side,
                        "size": order.size_matched,
                        "price": order.price
                    })
                    self.fills_received += 1
                    
                    # Update inventory
                    self.inventory.process_fill(
                        token_id=order.token_id,
                        side=order.side,
                        shares=order.size_matched,
                        price=order.price
                    )
            
            # Reset mismatch counter on successful reconcile
            self.safety.state.reconcile_mismatch_count = 0
            
        except Exception as e:
            self._log("RECONCILE_ERROR", str(e))
            self.safety.state.reconcile_mismatch_count += 1
    
    def _enforce_exits(self, yes_book: Optional[OrderBook], no_book: Optional[OrderBook]):
        """
        EXIT ENFORCEMENT - runs EVERY tick.
        Invariant: IF inventory > 0 THEN exit order MUST exist.
        """
        if not self.current_market:
            return
        
        # Check YES inventory
        yes_inv = self.inventory.get_yes_shares()
        if yes_inv > 0:
            has_exit = self.order_manager.has_exit_order(self.current_market.yes_token_id)
            self.safety.update_inv_exit_tracking(self.current_market.yes_token_id, True, has_exit)
            
            if not has_exit and yes_book:
                self._place_exit(self.current_market.yes_token_id, yes_inv, yes_book, "YES")
            elif has_exit:
                self._reprice_exit_if_needed(self.current_market.yes_token_id, yes_book, "YES")
        else:
            self.safety.update_inv_exit_tracking(self.current_market.yes_token_id, False, False)
        
        # Check NO inventory
        no_inv = self.inventory.get_no_shares()
        if no_inv > 0:
            has_exit = self.order_manager.has_exit_order(self.current_market.no_token_id)
            self.safety.update_inv_exit_tracking(self.current_market.no_token_id, True, has_exit)
            
            if not has_exit and no_book:
                self._place_exit(self.current_market.no_token_id, no_inv, no_book, "NO")
            elif has_exit:
                self._reprice_exit_if_needed(self.current_market.no_token_id, no_book, "NO")
        else:
            self.safety.update_inv_exit_tracking(self.current_market.no_token_id, False, False)
    
    def _place_exit(self, token_id: str, shares: float, book: OrderBook, label: str):
        """Place exit order at best_ask - 1 tick"""
        # Exit at best_ask - 1 tick (aggressive maker sell)
        exit_price = round(book.best_ask - 0.01, 2)
        exit_price = max(0.01, min(0.99, exit_price))
        
        quote = Quote(
            price=exit_price,
            size=min(shares, self.config.quoting.base_quote_size),
            side="SELL"
        )
        
        result = self.order_manager.place_or_replace(token_id, quote, role=OrderRole.EXIT)
        
        if result and result.success:
            self._exit_placed_time[token_id] = time.time()
            self.exits_placed += 1
            self._log("EXIT_POSTED", f"{label} SELL @ {exit_price:.2f} x {quote.size:.1f}", {
                "token": token_id[:20],
                "price": exit_price,
                "size": quote.size
            })
    
    def _reprice_exit_if_needed(self, token_id: str, book: Optional[OrderBook], label: str):
        """
        Reprice exit based on schedule:
        - 0-3s: stay at best_ask - 1 tick
        - 3-6s: reprice to best_ask
        - 6s+: reprice to best_bid + 1 tick (still postOnly)
        """
        if not book:
            return
        
        exit_order = self.order_manager.get_exit_order(token_id)
        if not exit_order:
            return
        
        placed_time = self._exit_placed_time.get(token_id, time.time())
        elapsed = time.time() - placed_time
        
        # Determine target price
        if elapsed < 3.0:
            target_price = round(book.best_ask - 0.01, 2)
        elif elapsed < 6.0:
            target_price = round(book.best_ask, 2)
        else:
            # Aggressive: best_bid + 1 tick (still maker)
            target_price = round(book.best_bid + 0.01, 2)
            if self.flatten_mode:
                # Even more aggressive in flatten mode
                target_price = round(book.mid, 2)
        
        target_price = max(0.01, min(0.99, target_price))
        
        # Check if reprice needed
        if abs(exit_order.price - target_price) >= 0.01:
            inv = self.inventory.get_yes_shares() if "YES" in label else self.inventory.get_no_shares()
            
            quote = Quote(
                price=target_price,
                size=min(inv, self.config.quoting.base_quote_size),
                side="SELL"
            )
            
            result = self.order_manager.place_or_replace(token_id, quote, role=OrderRole.EXIT)
            
            if result and result.success:
                self._log("EXIT_REPRICED", f"{label} SELL @ {target_price:.2f} (was {exit_order.price:.2f}, elapsed {elapsed:.1f}s)", {
                    "old_price": exit_order.price,
                    "new_price": target_price,
                    "elapsed": elapsed
                })
    
    def _entry_cycle(self, yes_book: Optional[OrderBook], no_book: Optional[OrderBook]):
        """Place entry orders (only if not flattening)"""
        if not self.current_market or not yes_book or not no_book:
            return
        
        # Get current account snapshot for sizing
        if not self._current_snapshot:
            self._current_snapshot = self.balance_mgr.get_snapshot()
        snap = self._current_snapshot
        
        # CRITICAL: Use spendable cash, not portfolio value
        if snap.spendable_usdc < self.balance_mgr.min_notional:
            self._log("SKIP_LOW_CASH", f"Spendable ${snap.spendable_usdc:.2f} < min notional ${self.balance_mgr.min_notional:.2f}")
            return
        
        # Check for spikes
        yes_cooldown = self._check_spike(self.current_market.yes_token_id, yes_book.mid)
        no_cooldown = self._check_spike(self.current_market.no_token_id, no_book.mid)
        
        if yes_cooldown and no_cooldown:
            return  # Both in cooldown
        
        # Sanity check: don't quote on empty books
        if not yes_book.has_liquidity or not no_book.has_liquidity:
            self._log("SKIP_NO_LIQUIDITY", "Books have no real liquidity")
            return
        
        # Helper to check and place entry bid
        def try_place_entry(token_id: str, book: OrderBook, label: str, cooldown: bool):
            if cooldown:
                return
            
            # Get base quote from engine
            quotes = self.quote_engine.compute_quotes(
                book=book,
                inventory_shares=self.inventory.get_yes_shares() if label == "YES" else self.inventory.get_no_shares(),
                max_inventory=self.config.risk.max_inv_shares_per_token,
                usdc_available=snap.spendable_usdc
            )
            
            if not quotes.bid:
                return
            
            price = quotes.bid.price
            size = quotes.bid.size
            notional = price * size
            
            # Check minimum notional
            if notional < self.balance_mgr.min_notional:
                required_size = self.balance_mgr.get_required_size_for_min_notional(price)
                
                # Check if required size violates caps
                if required_size > self.config.risk.max_inv_shares_per_token:
                    self._log("SKIP_MIN_NOTIONAL", f"{label}: price={price:.2f}, need size={required_size}, max_shares={self.config.risk.max_inv_shares_per_token}")
                    self.skipped_min_notional += 1
                    return
                
                required_notional = price * required_size
                if required_notional > snap.spendable_usdc:
                    self._log("SKIP_MIN_NOTIONAL", f"{label}: price={price:.2f}, need size={required_size}, notional=${required_notional:.2f} > spendable ${snap.spendable_usdc:.2f}")
                    self.skipped_min_notional += 1
                    return
                
                # Adjust size to meet minimum
                size = required_size
                notional = price * size
            
            # Check balance manager approval
            can_place, reason, adjusted_size = self.balance_mgr.can_place_buy(price, size, snap)
            if not can_place:
                if "SKIP_MIN_NOTIONAL" in reason:
                    self._log("SKIP_MIN_NOTIONAL", f"{label}: {reason}")
                    self.skipped_min_notional += 1
                else:
                    self._log("SKIP_BALANCE", f"{label}: {reason}")
                return
            
            if adjusted_size > 0 and adjusted_size != size:
                size = adjusted_size
            
            # Validate quote
            quote = Quote(price=price, size=size, side="BUY")
            valid, _ = self.quote_engine.validate_quote(quote, book)
            if not valid:
                return
            
            # Check inventory limits
            can_buy, reason = self.inventory.can_buy(token_id, size, price)
            if not can_buy:
                self._log("SKIP_INV_LIMIT", f"{label}: {reason}")
                return
            
            # Place order
            result = self.order_manager.place_or_replace(token_id, quote, role=OrderRole.ENTRY)
            if result and result.success:
                self._log("ENTRY_POSTED", f"{label} BID @ {price:.2f} x {size:.1f} (notional ${notional:.2f})")
                self.orders_placed += 1
        
        # Try YES entry
        try_place_entry(self.current_market.yes_token_id, yes_book, "YES", yes_cooldown)
        
        # Try NO entry
        try_place_entry(self.current_market.no_token_id, no_book, "NO", no_cooldown)
    
    def _check_spike(self, token_id: str, current_mid: float) -> bool:
        """Check for price spikes"""
        now = time.time()
        
        if token_id in self._cooldown_until:
            if now < self._cooldown_until[token_id]:
                return True
            else:
                del self._cooldown_until[token_id]
        
        if token_id in self._last_mid and token_id in self._last_mid_time:
            time_diff = now - self._last_mid_time[token_id]
            if time_diff <= self._spike_window_secs:
                price_move = abs(current_mid - self._last_mid[token_id])
                if price_move >= self._spike_threshold:
                    self._log("SPIKE", f"Token {token_id[:20]}... moved {price_move*100:.1f}c")
                    self._cooldown_until[token_id] = now + self._cooldown_secs
                    # Cancel entry orders (not exits!)
                    self.order_manager.cancel(token_id, "BUY")
                    return True
        
        self._last_mid[token_id] = current_mid
        self._last_mid_time[token_id] = now
        return False
    
    def _check_safety(self):
        """Check safety conditions"""
        should_kill, reason = self.safety.check_kill_conditions()
        if should_kill:
            self.safety.trigger_kill(reason)
            self.order_manager.cancel_all()
    
    def stop(self):
        """Stop the bot safely"""
        self._log("STOP", "Stopping bot...")
        self.running = False
        
        # Cancel all orders
        if self.config.mode != RunMode.DRYRUN:
            self.order_manager.cancel_all()
        
        # Print summary
        self._print_summary()
        
        # Cleanup
        self.safety.cleanup()
    
    def _print_summary(self):
        """Print final summary with complete account state"""
        elapsed = time.time() - self.start_time if self.start_time > 0 else 0
        om = self.order_manager.get_metrics()
        inv = self.inventory.get_summary()
        
        # Get final account snapshot
        final_snap = self.balance_mgr.get_snapshot()
        
        self._log("SUMMARY", "=" * 60)
        self._log("SUMMARY", "FINAL SUMMARY")
        self._log("SUMMARY", "=" * 60)
        
        # Runtime stats
        self._log("SUMMARY", f"Duration: {elapsed:.1f}s")
        self._log("SUMMARY", f"Ticks: {self.ticks}")
        
        # Order stats
        self._log("SUMMARY", "-" * 40)
        self._log("SUMMARY", "ORDER STATS")
        self._log("SUMMARY", f"  Orders placed: {self.orders_placed}")
        self._log("SUMMARY", f"  Fills received: {self.fills_received}")
        self._log("SUMMARY", f"  Exits placed: {self.exits_placed}")
        self._log("SUMMARY", f"  Skipped (min notional): {self.skipped_min_notional}")
        self._log("SUMMARY", f"  Reconciles: {self.reconciles}")
        
        # Final positions
        self._log("SUMMARY", "-" * 40)
        self._log("SUMMARY", "FINAL POSITIONS")
        self._log("SUMMARY", f"  YES shares: {inv['yes_shares']:.1f}")
        self._log("SUMMARY", f"  NO shares: {inv['no_shares']:.1f}")
        self._log("SUMMARY", f"  Open orders: {om['active_orders']}")
        
        # Account snapshot (CRITICAL)
        self._log("SUMMARY", "-" * 40)
        self._log("SUMMARY", "FINAL ACCOUNT (portfolio vs cash)")
        self._log("SUMMARY", f"  Cash available (spendable): ${final_snap.cash_available_usdc:.2f}")
        self._log("SUMMARY", f"  Locked in open buys:        ${final_snap.locked_usdc_in_open_buys:.2f}")
        self._log("SUMMARY", f"  Positions MTM:              ${final_snap.positions_mtm_usdc:.2f}")
        self._log("SUMMARY", f"  Equity estimate (portfolio): ${final_snap.equity_estimate_usdc:.2f}")
        self._log("SUMMARY", f"  Spendable:                   ${final_snap.spendable_usdc:.2f}")
        
        # Safety state
        self._log("SUMMARY", "-" * 40)
        self._log("SUMMARY", "SAFETY STATE")
        self._log("SUMMARY", f"  Flatten triggered: {self.flatten_triggered}")
        self._log("SUMMARY", f"  Flatten mode active: {self.flatten_mode}")
        self._log("SUMMARY", f"  Kill triggered: {self.safety.is_killed()}")
        if self.safety.is_killed():
            self._log("SUMMARY", f"  Kill reason: {self.safety.state.kill_reason}")
        
        self._log("SUMMARY", "=" * 60)
        
        # Log final account snapshot as data
        self._log("FINAL_ACCOUNT", final_snap.to_log_string(), final_snap.to_dict())


def main():
    """Main entry point"""
    import argparse
    import os
    
    parser = argparse.ArgumentParser(description="Polymarket MM Bot V2 - SAFE")
    parser.add_argument("--seconds", type=float, default=60, help="Run duration")
    parser.add_argument("--config", default="pm_api_config.json", help="Config file")
    args = parser.parse_args()
    
    # Load config
    config = Config.from_env(args.config)
    
    # Check for LIVE mode
    if os.environ.get("LIVE") == "1":
        config.mode = RunMode.LIVE
    
    # Run
    runner = MMRunnerV2(config)
    runner.run(duration_seconds=args.seconds)


if __name__ == "__main__":
    main()

