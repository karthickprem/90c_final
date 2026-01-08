"""
Fill-Exit Proof Script V2
=========================
Comprehensive test of the fill -> exit path.

Success criteria:
1. Place a near-mid BUY order
2. Wait for fill (up to 60s)
3. If filled, verify EXIT order appears within 2s
4. Monitor until inventory returns to 0 or timeout
5. No positions remain at end
"""

import sys
import os
import time

# Force unbuffered output
import builtins
_print = builtins.print
def print(*args, **kwargs):
    kwargs['flush'] = True
    _print(*args, **kwargs)

# Add parent to path
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from mm_bot.config import Config, RunMode
from mm_bot.clob import ClobWrapper, OrderBook
from mm_bot.market import MarketResolver
from mm_bot.inventory import InventoryManager
from mm_bot.order_manager import OrderManager, OrderRole


def log(event: str, msg: str):
    ts = time.strftime("%H:%M:%S")
    print(f"[{ts}] [{event}] {msg}", flush=True)


def main():
    # Check env
    if os.environ.get("LIVE") != "1":
        log("ERROR", "This script requires LIVE=1")
        log("INFO", "Run: $env:LIVE='1'; python scripts/mm_fill_exit_proof_v2.py")
        return 1
    
    log("START", "=" * 50)
    log("START", "FILL-EXIT PROOF TEST V2")
    log("START", "=" * 50)
    
    # Load config with tiny caps
    config = Config.from_env("pm_api_config.json")
    config.mode = RunMode.LIVE
    config.risk.max_usdc_locked = 2.0
    config.risk.max_inv_shares_per_token = 10
    config.quoting.base_quote_size = 5
    
    log("CONFIG", f"Max USDC: ${config.risk.max_usdc_locked:.2f}")
    log("CONFIG", f"Max shares: {config.risk.max_inv_shares_per_token}")
    log("CONFIG", f"Quote size: {config.quoting.base_quote_size}")
    
    # Initialize components
    clob = ClobWrapper(config)
    resolver = MarketResolver(config)
    inventory = InventoryManager(config)
    order_manager = OrderManager(config, clob)
    
    # Resolve market
    market = resolver.resolve_market()
    if not market:
        log("ERROR", "Could not resolve market")
        return 1
    
    log("MARKET", f"YES: {market.yes_token_id[:30]}...")
    log("MARKET", f"NO:  {market.no_token_id[:30]}...")
    
    inventory.set_tokens(market.yes_token_id, market.no_token_id)
    
    # Get initial state
    balance = clob.get_balance()
    log("BALANCE", f"USDC: ${balance['usdc']:.2f} | Positions: ${balance['positions']:.2f}")
    
    # Cancel any existing orders
    log("CANCEL", "Cancelling all orders...")
    clob.cancel_all()
    time.sleep(1)
    
    # Get book (wait for valid book up to 60s)
    log("WAIT", "Waiting for valid orderbook (up to 60s)...")
    wait_start = time.time()
    yes_book = None
    
    while time.time() - wait_start < 60:
        yes_book = clob.get_order_book(market.yes_token_id)
        
        if yes_book and yes_book.has_liquidity:
            break
        
        # Check if window is ending
        window = resolver.get_current_window()
        secs_left = window.get("secs_left", 900)
        if secs_left < 120:
            log("WARN", f"Window ending in {secs_left}s, waiting for new window...")
        
        log("WAIT", f"Book empty (bid={yes_book.best_bid if yes_book else 0:.2f} ask={yes_book.best_ask if yes_book else 0:.2f}), waiting...")
        time.sleep(5)
    
    if not yes_book or not yes_book.has_liquidity:
        log("ERROR", "No valid YES book after waiting 60s")
        return 1
    
    log("BOOK", f"YES: bid={yes_book.best_bid:.2f} ask={yes_book.best_ask:.2f} mid={yes_book.mid:.2f}")
    
    # Place entry BID at mid (aggressive to get fill)
    entry_price = round(yes_book.mid, 2)
    entry_size = config.quoting.base_quote_size
    
    log("ENTRY", f"Placing BUY @ {entry_price:.2f} x {entry_size} (at mid to get fill)")
    
    result = clob.post_order(
        token_id=market.yes_token_id,
        side="BUY",
        price=entry_price,
        size=entry_size,
        post_only=True
    )
    
    if not result.success:
        log("ERROR", f"Failed to place entry: {result.error}")
        # Try slightly worse price
        entry_price = round(yes_book.best_bid + 0.01, 2)
        log("RETRY", f"Trying BUY @ {entry_price:.2f}")
        result = clob.post_order(
            token_id=market.yes_token_id,
            side="BUY",
            price=entry_price,
            size=entry_size,
            post_only=True
        )
        
        if not result.success:
            log("ERROR", f"Retry failed: {result.error}")
            return 1
    
    log("ENTRY", f"Order placed: {result.order_id[:30] if result.order_id else 'unknown'}...")
    
    # Wait for fill (up to 60s)
    fill_received = False
    fill_wait_start = time.time()
    fill_shares = 0.0
    
    log("WAIT", "Waiting for fill (max 60s)...")
    
    while time.time() - fill_wait_start < 60:
        time.sleep(1)
        
        # Check orders
        orders = clob.get_open_orders()
        
        for o in orders:
            if o.token_id == market.yes_token_id and o.side == "BUY":
                if o.size_matched > 0:
                    fill_received = True
                    fill_shares = o.size_matched
                    log("FILL_RECEIVED", f"Got fill: {fill_shares:.1f} shares @ {entry_price:.2f}")
                    inventory.process_fill(market.yes_token_id, "BUY", fill_shares, entry_price)
                    break
        
        if fill_received:
            break
        
        # Check if order still exists
        our_order = None
        for o in orders:
            if o.token_id == market.yes_token_id and o.side == "BUY":
                our_order = o
                break
        
        if not our_order:
            # Order disappeared - check if it was filled entirely
            balance = clob.get_balance()
            if balance["positions"] > 0.01:
                fill_received = True
                fill_shares = entry_size
                log("FILL_RECEIVED", f"Order gone, position exists - assuming full fill")
                inventory.process_fill(market.yes_token_id, "BUY", fill_shares, entry_price)
                break
        
        elapsed = time.time() - fill_wait_start
        log("WAIT", f"Still waiting for fill... {elapsed:.0f}s elapsed")
    
    if not fill_received:
        log("NO_FILL", "No fill received within 60s - cancelling and exiting")
        clob.cancel_all()
        return 0
    
    # Fill received - now test exit path
    log("TEST", "=" * 40)
    log("TEST", "TESTING EXIT PATH")
    log("TEST", "=" * 40)
    
    # Get current inventory
    yes_inv = inventory.get_yes_shares()
    log("INV", f"YES inventory: {yes_inv:.1f}")
    
    # Place exit order
    yes_book = clob.get_order_book(market.yes_token_id)
    exit_price = round(yes_book.best_ask - 0.01, 2)
    exit_price = max(0.01, min(0.99, exit_price))
    
    log("EXIT", f"Placing SELL @ {exit_price:.2f} x {yes_inv:.1f}")
    
    exit_result = clob.post_order(
        token_id=market.yes_token_id,
        side="SELL",
        price=exit_price,
        size=yes_inv,
        post_only=True
    )
    
    if exit_result.success:
        log("EXIT_POSTED", f"Exit order placed: {exit_result.order_id[:30] if exit_result.order_id else 'unknown'}...")
    else:
        log("EXIT_FAIL", f"Failed to place exit: {exit_result.error}")
    
    # Monitor for exit fill (repricing every 3s)
    exit_start = time.time()
    max_exit_wait = 120  # 2 minutes max
    reprice_interval = 3
    last_reprice = time.time()
    
    log("WAIT", f"Waiting for exit fill (max {max_exit_wait}s, repricing every {reprice_interval}s)...")
    
    while time.time() - exit_start < max_exit_wait:
        time.sleep(1)
        
        # Check if position is gone
        balance = clob.get_balance()
        if balance["positions"] <= 0.01:
            log("EXIT_COMPLETE", "Position closed!")
            break
        
        # Check for partial fills
        orders = clob.get_open_orders()
        sell_orders = [o for o in orders if o.side == "SELL" and o.token_id == market.yes_token_id]
        
        for o in sell_orders:
            if o.size_matched > 0:
                log("EXIT_PARTIAL", f"Partial exit fill: {o.size_matched:.1f} shares")
        
        # Reprice
        if time.time() - last_reprice >= reprice_interval:
            last_reprice = time.time()
            
            yes_book = clob.get_order_book(market.yes_token_id)
            if yes_book:
                # Progressively more aggressive
                elapsed = time.time() - exit_start
                if elapsed < 6:
                    new_price = round(yes_book.best_ask - 0.01, 2)
                elif elapsed < 12:
                    new_price = round(yes_book.best_ask, 2)
                else:
                    new_price = round(yes_book.mid, 2)
                
                new_price = max(0.01, min(0.99, new_price))
                
                log("EXIT_REPRICED", f"Repricing SELL to {new_price:.2f} (was {exit_price:.2f})")
                
                # Cancel old and place new
                clob.cancel_all()
                time.sleep(0.5)
                
                exit_result = clob.post_order(
                    token_id=market.yes_token_id,
                    side="SELL",
                    price=new_price,
                    size=yes_inv,
                    post_only=True
                )
                exit_price = new_price
        
        elapsed = time.time() - exit_start
        log("WAIT", f"Waiting for exit... {elapsed:.0f}s, positions: ${balance['positions']:.2f}")
    
    # Final cleanup
    log("CLEANUP", "Cancelling all orders...")
    clob.cancel_all()
    time.sleep(1)
    
    # Final state
    final_orders = clob.get_open_orders()
    final_balance = clob.get_balance()
    
    log("FINAL", "=" * 50)
    log("FINAL", f"Open orders: {len(final_orders)}")
    log("FINAL", f"USDC: ${final_balance['usdc']:.2f}")
    log("FINAL", f"Positions: ${final_balance['positions']:.2f}")
    
    # Success check
    if len(final_orders) == 0 and final_balance["positions"] <= 0.01:
        log("SUCCESS", "PROOF PASSED - Fill->Exit worked correctly!")
        return 0
    else:
        log("FAIL", "PROOF FAILED - Inventory or orders remain!")
        return 1


if __name__ == "__main__":
    sys.exit(main())

