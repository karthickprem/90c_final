"""
Main Runner
===========
Main loop with states DRYRUN/PAPER/LIVE.
Metrics, logging, and kill switch.
"""

import sys
import time
import json
import signal
from datetime import datetime
from typing import Optional, Dict
from pathlib import Path

from .config import Config, RunMode
from .clob import ClobWrapper, OrderBook
from .market import MarketResolver, MarketInfo
from .inventory import InventoryManager
from .quoting import QuoteEngine, Quote
from .order_manager import OrderManager
from .ws_user import UserWebSocket, FillEvent, OrderEvent


class MMRunner:
    """
    Market Making Bot Runner
    
    States:
    - DRYRUN: No orders, just log what would happen
    - PAPER: Simulated fills
    - LIVE: Real orders
    """
    
    def __init__(self, config: Config):
        self.config = config
        
        # Components
        self.clob = ClobWrapper(config)
        self.market_resolver = MarketResolver(config)
        self.inventory = InventoryManager(config)
        self.quote_engine = QuoteEngine(config)
        self.order_manager = OrderManager(config, self.clob)
        self.ws: Optional[UserWebSocket] = None
        
        # State
        self.running = False
        self.current_market: Optional[MarketInfo] = None
        self.last_window_slug: Optional[str] = None
        
        # Spike detection (no-quote during rapid price moves)
        self._last_mid: Dict[str, float] = {}  # token_id -> last mid
        self._last_mid_time: Dict[str, float] = {}  # token_id -> timestamp
        self._cooldown_until: Dict[str, float] = {}  # token_id -> cooldown end time
        self._spike_threshold = 0.02  # 2c move triggers cooldown
        self._spike_window_secs = 5.0  # Check over 5 seconds
        self._cooldown_secs = 10.0  # Pause quoting for 10s after spike
        
        # Reconciliation
        self._last_reconcile = 0.0
        self._reconcile_interval = 10.0  # Reconcile every 10 seconds
        
        # Flatten rule
        self._flatten_threshold_secs = 120  # Flatten when < 120s to settlement
        
        # Metrics
        self.start_time = 0.0
        self.ticks = 0
        self.quotes_computed = 0
        self.orders_placed = 0
        self.guardrail_triggers = 0
        
        # Logging
        self.log_file: Optional[Path] = None
        if config.log_file:
            self.log_file = Path(config.log_file)
        
        # Signal handling
        signal.signal(signal.SIGINT, self._signal_handler)
        signal.signal(signal.SIGTERM, self._signal_handler)
    
    def _signal_handler(self, signum, frame):
        """Handle shutdown signals"""
        print("\n[RUNNER] Shutdown signal received")
        self.stop()
    
    def _log(self, msg: str, level: str = "INFO"):
        """Log message to console and file"""
        ts = datetime.now().strftime("%H:%M:%S.%f")[:-3]
        line = f"[{ts}] [{level}] {msg}"
        
        if self.config.verbose or level in ["ERROR", "WARN"]:
            print(line)
        
        if self.log_file:
            entry = {
                "ts": datetime.now().isoformat(),
                "level": level,
                "msg": msg
            }
            with open(self.log_file, "a") as f:
                f.write(json.dumps(entry) + "\n")
    
    def _log_event(self, event_type: str, data: dict):
        """Log structured event"""
        if self.log_file:
            entry = {
                "ts": datetime.now().isoformat(),
                "event": event_type,
                **data
            }
            with open(self.log_file, "a") as f:
                f.write(json.dumps(entry) + "\n")
    
    def _on_fill(self, event: FillEvent):
        """Handle fill from WebSocket"""
        self._log(f"FILL: {event.side} {event.size:.2f} @ {event.price:.4f}")
        
        # Update inventory
        self.inventory.process_fill(
            token_id=event.token_id,
            side=event.side,
            shares=event.size,
            price=event.price
        )
        
        # Update order manager
        self.order_manager.update_from_fill(event.order_id, event.size)
        
        self._log_event("fill", {
            "order_id": event.order_id,
            "side": event.side,
            "size": event.size,
            "price": event.price
        })
    
    def _on_order_update(self, event: OrderEvent):
        """Handle order status from WebSocket"""
        self.order_manager.update_from_fill(event.order_id, event.size_matched)
    
    def setup(self):
        """Initialize components"""
        self._log("=" * 60)
        self._log("POLYMARKET MARKET MAKING BOT")
        self._log("=" * 60)
        
        self.config.print_summary()
        
        # Validate config
        errors = self.config.validate()
        if errors:
            for err in errors:
                self._log(f"Config error: {err}", "ERROR")
            if self.config.mode == RunMode.LIVE:
                raise ValueError("Invalid config for LIVE mode")
        
        # Resolve market
        self.current_market = self.market_resolver.resolve_market()
        if not self.current_market:
            self._log("Could not resolve market tokens", "ERROR")
            raise ValueError("Market resolution failed")
        
        self._log(f"Market: {self.current_market.slug}")
        self._log(f"YES token: {self.current_market.yes_token_id[:20]}...")
        self._log(f"NO token: {self.current_market.no_token_id[:20]}...")
        
        # Set up inventory
        self.inventory.set_tokens(
            self.current_market.yes_token_id,
            self.current_market.no_token_id
        )
        
        # Get initial balance
        if self.config.mode != RunMode.DRYRUN:
            balance = self.clob.get_balance()
            self.inventory.reconcile(
                usdc_balance=balance["usdc"],
                position_value=balance["positions"]
            )
            self._log(f"Balance: ${balance['usdc']:.2f} + ${balance['positions']:.2f} positions")
        
        # Start WebSocket for fills (LIVE mode only)
        # NOTE: Disabled due to authentication issues - using REST polling instead
        # if self.config.mode == RunMode.LIVE:
        #     self.ws = UserWebSocket(
        #         self.config,
        #         on_fill=self._on_fill,
        #         on_order=self._on_order_update
        #     )
        #     self.ws.start()
        
        self._log("Setup complete")
        self._log("=" * 60)
    
    def run(self, duration_seconds: float = 60.0):
        """Main run loop"""
        self.setup()
        self.running = True
        self.start_time = time.time()
        deadline = self.start_time + duration_seconds
        
        self._log(f"Starting main loop for {duration_seconds:.0f}s")
        self._log(f"Mode: {self.config.mode.value.upper()}")
        
        try:
            while self.running and time.time() < deadline:
                tick_start = time.time()
                self.ticks += 1
                
                # Check if window changed
                window = self.market_resolver.get_current_window()
                if window["slug"] != self.last_window_slug:
                    self._handle_window_change(window)
                    self.last_window_slug = window["slug"]
                
                # Periodic reconciliation (always runs)
                self._reconcile_positions()
                
                # Manage exits for any inventory (always runs, even in cooldown)
                self._manage_exits()
                
                # Check if we should flatten (near end of window)
                in_flatten_mode = self._check_flatten(window)
                
                # Run quote cycle (skip if flattening)
                if not in_flatten_mode:
                    self._quote_cycle()
                
                # Check risk limits
                if not self._check_risk():
                    break
                
                # Print status
                self._print_status(window)
                
                # Sleep to target poll interval
                elapsed = time.time() - tick_start
                sleep_time = max(0, self.config.market.poll_interval - elapsed)
                if sleep_time > 0:
                    time.sleep(sleep_time)
        
        except Exception as e:
            self._log(f"Error in main loop: {e}", "ERROR")
            import traceback
            traceback.print_exc()
        
        finally:
            self.stop()
    
    def _handle_window_change(self, window: dict):
        """Handle 15-min window transition"""
        self._log(f"New window: {window['slug']}")
        
        # Cancel all orders from previous window
        self.order_manager.cancel_all()
        
        # Resolve new market
        self.current_market = self.market_resolver.resolve_market(window["slug"])
        
        if self.current_market:
            self.inventory.set_tokens(
                self.current_market.yes_token_id,
                self.current_market.no_token_id
            )
    
    def _reconcile_positions(self):
        """
        Periodic reconciliation: fetch actual positions from REST and 
        compare to internal inventory. Overwrite if mismatch.
        """
        now = time.time()
        if now - self._last_reconcile < self._reconcile_interval:
            return
        
        self._last_reconcile = now
        
        try:
            # Fetch actual balance/positions
            balance = self.clob.get_balance()
            self.inventory.reconcile(
                usdc_balance=balance["usdc"],
                position_value=balance["positions"]
            )
            
            # TODO: Fetch actual token positions via positions API
            # For now, sync from open orders to detect fills
            api_orders = self.clob.get_open_orders()
            self.order_manager.sync_with_api(api_orders)
            
            # Check for position mismatches by comparing order fill states
            for order in api_orders:
                if order.size_matched > 0:
                    # A fill happened - update inventory
                    self._log(f"RECONCILE: Detected fill on {order.side} {order.size_matched:.1f} shares", "WARN")
                    self.inventory.process_fill(
                        token_id=order.token_id,
                        side=order.side,
                        shares=order.size_matched,
                        price=order.price
                    )
        except Exception as e:
            self._log(f"Reconcile error: {e}", "ERROR")
    
    def _manage_exits(self):
        """
        Manage exit orders for any inventory we hold.
        This runs ALWAYS, even during cooldown.
        """
        if not self.current_market:
            return
        
        # Check YES inventory
        yes_shares = self.inventory.get_yes_shares()
        if yes_shares > 0:
            self._ensure_exit_order(
                self.current_market.yes_token_id,
                yes_shares,
                "YES"
            )
        
        # Check NO inventory
        no_shares = self.inventory.get_no_shares()
        if no_shares > 0:
            self._ensure_exit_order(
                self.current_market.no_token_id,
                no_shares,
                "NO"
            )
    
    def _ensure_exit_order(self, token_id: str, shares: float, label: str):
        """Ensure an exit (SELL) order exists for our inventory"""
        from .quoting import Quote
        from .clob import Side
        
        # Check if we already have a sell order
        existing = self.order_manager.get_order(token_id, "SELL")
        if existing and existing.is_active:
            return  # Already have exit order
        
        # Get current book to price the exit
        book = self.clob.get_order_book(token_id)
        if not book:
            return
        
        # Price exit at mid + small edge (maker sell)
        exit_price = round(book.mid + self.config.quoting.target_half_spread_cents / 100, 2)
        exit_price = max(0.01, min(0.99, exit_price))
        
        # Create exit quote
        exit_quote = Quote(
            price=exit_price,
            size=min(shares, self.config.quoting.base_quote_size),
            side="SELL"
        )
        
        self._log(f"EXIT: Placing SELL {label} @ {exit_price:.2f} x {exit_quote.size:.1f}", "INFO")
        result = self.order_manager.place_or_replace(token_id, exit_quote)
        
        if result and result.success:
            self.orders_placed += 1
    
    def _check_flatten(self, window: dict) -> bool:
        """
        Check if we should flatten positions (near end of window).
        Returns True if in flatten mode.
        """
        secs_left = window.get("secs_left", 900)
        
        if secs_left < self._flatten_threshold_secs:
            # Cancel all entry orders
            if self.current_market:
                self.order_manager.cancel(self.current_market.yes_token_id, "BUY")
                self.order_manager.cancel(self.current_market.no_token_id, "BUY")
            
            # Flatten any inventory
            self._flatten_inventory()
            return True
        
        return False
    
    def _flatten_inventory(self):
        """Aggressively flatten inventory near end of window"""
        if not self.current_market:
            return
        
        from .quoting import Quote
        
        # Flatten YES
        yes_shares = self.inventory.get_yes_shares()
        if yes_shares > 0:
            book = self.clob.get_order_book(self.current_market.yes_token_id)
            if book:
                # Price aggressively to ensure fill
                flatten_price = round(book.mid, 2)
                flatten_price = max(0.01, min(0.99, flatten_price))
                
                quote = Quote(price=flatten_price, size=yes_shares, side="SELL")
                self._log(f"FLATTEN: Selling YES @ {flatten_price:.2f} x {yes_shares:.1f}", "WARN")
                self.order_manager.place_or_replace(self.current_market.yes_token_id, quote)
        
        # Flatten NO
        no_shares = self.inventory.get_no_shares()
        if no_shares > 0:
            book = self.clob.get_order_book(self.current_market.no_token_id)
            if book:
                flatten_price = round(book.mid, 2)
                flatten_price = max(0.01, min(0.99, flatten_price))
                
                quote = Quote(price=flatten_price, size=no_shares, side="SELL")
                self._log(f"FLATTEN: Selling NO @ {flatten_price:.2f} x {no_shares:.1f}", "WARN")
                self.order_manager.place_or_replace(self.current_market.no_token_id, quote)
    
    def _check_spike(self, token_id: str, current_mid: float) -> bool:
        """
        Check if there's been a rapid price move (spike).
        Returns True if we should skip quoting (in cooldown).
        """
        now = time.time()
        
        # Check if in cooldown
        if token_id in self._cooldown_until:
            if now < self._cooldown_until[token_id]:
                return True  # Still in cooldown
            else:
                del self._cooldown_until[token_id]
        
        # Check for spike
        if token_id in self._last_mid and token_id in self._last_mid_time:
            time_diff = now - self._last_mid_time[token_id]
            if time_diff <= self._spike_window_secs:
                price_move = abs(current_mid - self._last_mid[token_id])
                if price_move >= self._spike_threshold:
                    self._log(f"SPIKE detected: {token_id[:20]}... moved {price_move*100:.1f}c in {time_diff:.1f}s", "WARN")
                    self._cooldown_until[token_id] = now + self._cooldown_secs
                    self.order_manager.cancel(token_id, "BUY")
                    self.order_manager.cancel(token_id, "SELL")
                    self.guardrail_triggers += 1
                    return True
        
        # Update tracking
        self._last_mid[token_id] = current_mid
        self._last_mid_time[token_id] = now
        return False
    
    def _quote_cycle(self):
        """Run one quote cycle"""
        if not self.current_market:
            return
        
        # Get order books for both tokens
        yes_book = self.clob.get_order_book(self.current_market.yes_token_id)
        no_book = self.clob.get_order_book(self.current_market.no_token_id)
        
        if not yes_book or not no_book:
            return
        
        # Check for spikes (cancel quotes and pause if price moving fast)
        yes_in_cooldown = self._check_spike(self.current_market.yes_token_id, yes_book.mid)
        no_in_cooldown = self._check_spike(self.current_market.no_token_id, no_book.mid)
        
        if yes_in_cooldown and no_in_cooldown:
            return  # Both in cooldown, skip quoting entirely
        
        # Compute quotes for YES token
        yes_quotes = self.quote_engine.compute_quotes(
            book=yes_book,
            inventory_shares=self.inventory.get_yes_shares(),
            max_inventory=self.config.risk.max_inv_shares_per_token,
            usdc_available=self.config.risk.max_usdc_locked - self.order_manager.get_locked_usdc()
        )
        
        # Compute quotes for NO token
        no_quotes = self.quote_engine.compute_quotes(
            book=no_book,
            inventory_shares=self.inventory.get_no_shares(),
            max_inventory=self.config.risk.max_inv_shares_per_token,
            usdc_available=self.config.risk.max_usdc_locked - self.order_manager.get_locked_usdc()
        )
        
        self.quotes_computed += 1
        
        # Log computed quotes (DRYRUN shows what would be posted)
        if self.config.mode == RunMode.DRYRUN:
            self._log_dryrun_quotes(yes_book, no_book, yes_quotes, no_quotes)
            return
        
        # Place/replace orders
        if yes_quotes.bid:
            valid, reason = self.quote_engine.validate_quote(yes_quotes.bid, yes_book)
            if valid:
                can_buy, _ = self.inventory.can_buy(
                    self.current_market.yes_token_id,
                    yes_quotes.bid.size,
                    yes_quotes.bid.price
                )
                if can_buy:
                    result = self.order_manager.place_or_replace(
                        self.current_market.yes_token_id,
                        yes_quotes.bid
                    )
                    if result and result.success:
                        self.orders_placed += 1
        
        if yes_quotes.ask:
            valid, reason = self.quote_engine.validate_quote(yes_quotes.ask, yes_book)
            if valid:
                can_sell, _ = self.inventory.can_sell(
                    self.current_market.yes_token_id,
                    yes_quotes.ask.size
                )
                if can_sell:
                    result = self.order_manager.place_or_replace(
                        self.current_market.yes_token_id,
                        yes_quotes.ask
                    )
                    if result and result.success:
                        self.orders_placed += 1
        
        # Same for NO token
        if no_quotes.bid:
            valid, reason = self.quote_engine.validate_quote(no_quotes.bid, no_book)
            if valid:
                can_buy, _ = self.inventory.can_buy(
                    self.current_market.no_token_id,
                    no_quotes.bid.size,
                    no_quotes.bid.price
                )
                if can_buy:
                    result = self.order_manager.place_or_replace(
                        self.current_market.no_token_id,
                        no_quotes.bid
                    )
                    if result and result.success:
                        self.orders_placed += 1
        
        if no_quotes.ask:
            valid, reason = self.quote_engine.validate_quote(no_quotes.ask, no_book)
            if valid:
                can_sell, _ = self.inventory.can_sell(
                    self.current_market.no_token_id,
                    no_quotes.ask.size
                )
                if can_sell:
                    result = self.order_manager.place_or_replace(
                        self.current_market.no_token_id,
                        no_quotes.ask
                    )
                    if result and result.success:
                        self.orders_placed += 1
    
    def _log_dryrun_quotes(self, yes_book, no_book, yes_quotes, no_quotes):
        """Log quotes in DRYRUN mode"""
        # Only log every 5 ticks to avoid spam (but show some output)
        if self.ticks % 5 != 0:
            return
        
        print(f"\n[DRYRUN] Tick {self.ticks}")
        print(f"  YES: bid={yes_book.best_bid:.2f} ask={yes_book.best_ask:.2f} spread={yes_book.spread*100:.1f}c")
        print(f"  NO:  bid={no_book.best_bid:.2f} ask={no_book.best_ask:.2f} spread={no_book.spread*100:.1f}c")
        
        if yes_quotes.bid:
            print(f"  -> Would BID YES @ {yes_quotes.bid.price:.2f} x {yes_quotes.bid.size:.0f}")
        if yes_quotes.ask:
            print(f"  -> Would ASK YES @ {yes_quotes.ask.price:.2f} x {yes_quotes.ask.size:.0f}")
        if no_quotes.bid:
            print(f"  -> Would BID NO @ {no_quotes.bid.price:.2f} x {no_quotes.bid.size:.0f}")
        if no_quotes.ask:
            print(f"  -> Would ASK NO @ {no_quotes.ask.price:.2f} x {no_quotes.ask.size:.0f}")
    
    def _check_risk(self) -> bool:
        """Check risk limits, return False if kill switch triggered"""
        trigger, reason = self.inventory.check_kill_switch()
        if trigger:
            self._log(f"KILL SWITCH: {reason}", "ERROR")
            self.order_manager.cancel_all()
            self.guardrail_triggers += 1
            return False
        return True
    
    def _print_status(self, window: dict):
        """Print status line"""
        if self.ticks % 5 != 0:
            return
        
        inv = self.inventory.get_summary()
        om = self.order_manager.get_metrics()
        
        status = f"[{window['secs_left']//60}:{window['secs_left']%60:02d}]"
        status += f" YES={inv['yes_shares']:.0f} NO={inv['no_shares']:.0f}"
        status += f" | Orders={om['active_orders']} Replaces={om['total_replaces']}"
        
        print(f"\r{status}  ", end="", flush=True)
    
    def stop(self):
        """Stop the bot"""
        self._log("Stopping...")
        self.running = False
        
        # Cancel all orders
        if self.config.mode != RunMode.DRYRUN:
            self.order_manager.cancel_all()
        
        # Stop WebSocket
        if self.ws:
            self.ws.stop()
        
        # Print summary
        self.print_summary()
    
    def print_summary(self):
        """Print final summary"""
        elapsed = time.time() - self.start_time if self.start_time > 0 else 0
        om = self.order_manager.get_metrics()
        inv = self.inventory.get_summary()
        
        print("\n")
        print("=" * 60)
        print("SUMMARY")
        print("=" * 60)
        print(f"Mode:              {self.config.mode.value.upper()}")
        print(f"Duration:          {elapsed:.1f}s")
        print(f"Ticks:             {self.ticks}")
        print(f"Quotes computed:   {self.quotes_computed}")
        print(f"Orders placed:     {self.orders_placed}")
        print(f"Order replaces:    {om['total_replaces']}")
        print(f"Post-only rejects: {om['replace_rejects']}")
        print(f"Guardrail triggers:{self.guardrail_triggers}")
        print()
        print("Inventory:")
        print(f"  YES shares:      {inv['yes_shares']:.1f}")
        print(f"  NO shares:       {inv['no_shares']:.1f}")
        print(f"  USDC available:  ${inv['usdc_available']:.2f}")
        print(f"  Locked in orders:${om['locked_usdc']:.2f}")
        print("=" * 60)


def main():
    """Main entry point"""
    import argparse
    
    parser = argparse.ArgumentParser(description="Polymarket MM Bot")
    parser.add_argument("--seconds", type=float, default=60, help="Run duration")
    parser.add_argument("--live", action="store_true", help="LIVE mode (real orders)")
    parser.add_argument("--paper", action="store_true", help="PAPER mode (simulated)")
    parser.add_argument("--config", default="pm_api_config.json", help="Config file")
    args = parser.parse_args()
    
    # Load config
    config = Config.from_env(args.config)
    
    # Override mode from args
    if args.live:
        config.mode = RunMode.LIVE
    elif args.paper:
        config.mode = RunMode.PAPER
    
    # Run
    runner = MMRunner(config)
    runner.run(duration_seconds=args.seconds)


if __name__ == "__main__":
    main()

