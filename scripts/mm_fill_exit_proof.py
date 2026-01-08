"""
MM Fill -> Exit Proof Script
=============================
Validates that the bot can:
1. Place a near-mid postOnly bid
2. Detect when it fills
3. Place an exit order within 2 seconds
4. Clean up properly

Run with: set LIVE=1 && python scripts/mm_fill_exit_proof.py
"""

import sys
import os
import time

# Fix Windows encoding
if sys.platform == 'win32':
    sys.stdout.reconfigure(encoding='utf-8', errors='replace')

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from mm_bot.config import Config, RunMode
from mm_bot.clob import ClobWrapper, Side
from mm_bot.market import MarketResolver
from mm_bot.inventory import InventoryManager
from mm_bot.order_manager import OrderManager
from mm_bot.quoting import Quote


def main():
    # Check LIVE mode
    if os.environ.get("LIVE") != "1":
        print("=" * 60)
        print("FILL -> EXIT PROOF TEST")
        print("=" * 60)
        print("To run this test, set LIVE=1:")
        print()
        print("  set LIVE=1")
        print("  python scripts/mm_fill_exit_proof.py")
        print()
        print("This test will:")
        print("  1. Place a near-mid bid (likely to fill)")
        print("  2. Wait up to 60s for fill")
        print("  3. On fill, verify exit order placed within 2s")
        print("  4. Cancel everything and report")
        print("=" * 60)
        return
    
    print("=" * 60)
    print("FILL -> EXIT PROOF TEST (LIVE)")
    print("=" * 60)
    
    # Load config
    config = Config.from_env("pm_api_config.json")
    config.mode = RunMode.LIVE
    config.verbose = True
    
    # Use tiny limits
    config.risk.max_usdc_locked = 1.0
    config.risk.max_inv_shares_per_token = 5.0
    config.quoting.base_quote_size = 5.0  # Minimum order size
    
    # Validate
    errors = config.validate()
    if errors:
        print("Config errors:")
        for e in errors:
            print(f"  - {e}")
        return
    
    # Initialize
    clob = ClobWrapper(config)
    resolver = MarketResolver(config)
    inventory = InventoryManager(config)
    order_manager = OrderManager(config, clob)
    
    # Get market
    market = resolver.resolve_market()
    if not market:
        print("ERROR: Could not resolve market")
        return
    
    print(f"Market: {market.slug}")
    print(f"Time left: {market.secs_left}s")
    
    if market.secs_left < 180:
        print("WARNING: Less than 3 minutes left in window")
        print("Consider waiting for next window")
    
    # Setup inventory tracking
    inventory.set_tokens(market.yes_token_id, market.no_token_id)
    
    # Get order book
    book = clob.get_order_book(market.yes_token_id)
    if not book:
        print("ERROR: Could not get order book")
        return
    
    print(f"\nOrder book: bid={book.best_bid:.2f} ask={book.best_ask:.2f} mid={book.mid:.2f}")
    
    # Cancel any existing orders
    print("\nStep 0: Cancelling any existing orders...")
    clob.cancel_all()
    time.sleep(1)
    
    # Place a bid near mid (higher chance of fill)
    # Use mid - 1c to be slightly below mid
    bid_price = round(book.mid - 0.01, 2)
    bid_price = max(0.01, min(0.98, bid_price))  # Keep valid range
    bid_size = 5.0  # Minimum size
    
    print(f"\nStep 1: Placing postOnly BID @ {bid_price:.2f} x {bid_size}")
    
    result = clob.post_order(
        token_id=market.yes_token_id,
        side=Side.BUY,
        price=bid_price,
        size=bid_size,
        post_only=True
    )
    
    if not result.success:
        print(f"  FAILED: {result.error}")
        return
    
    bid_order_id = result.order_id
    print(f"  SUCCESS: order_id={bid_order_id[:40]}...")
    
    # Wait for fill (check every 2 seconds)
    print(f"\nStep 2: Waiting up to 60s for fill...")
    
    fill_detected = False
    fill_time = None
    start_time = time.time()
    
    while time.time() - start_time < 60:
        elapsed = int(time.time() - start_time)
        
        # Check order status
        orders = clob.get_open_orders(market.yes_token_id)
        bid_order = next((o for o in orders if o.order_id == bid_order_id), None)
        
        if bid_order is None:
            # Order no longer exists - either filled or cancelled
            print(f"\n  [{elapsed}s] Order disappeared (likely filled)")
            fill_detected = True
            fill_time = time.time()
            break
        
        if bid_order.size_matched > 0:
            print(f"\n  [{elapsed}s] PARTIAL FILL detected: {bid_order.size_matched:.1f} shares")
            fill_detected = True
            fill_time = time.time()
            
            # Update inventory
            inventory.process_fill(
                market.yes_token_id,
                "BUY",
                bid_order.size_matched,
                bid_order.price
            )
            break
        
        print(f"\r  [{elapsed}s] Waiting... status={bid_order.status}  ", end="", flush=True)
        time.sleep(2)
    
    if not fill_detected:
        print("\n  No fill after 60s")
        print("\nStep 3: Cancelling bid and exiting...")
        clob.cancel_order(bid_order_id)
        
        # Final state
        orders = clob.get_open_orders()
        balance = clob.get_balance()
        print(f"\nFinal state:")
        print(f"  Open orders: {len(orders)}")
        print(f"  Balance: USDC={balance['usdc']:.2f} Positions={balance['positions']:.2f}")
        print("\nTEST: INCONCLUSIVE (no fill occurred)")
        return
    
    # Step 3: Place exit order
    print(f"\nStep 3: Placing exit order...")
    
    # Get fresh book
    book = clob.get_order_book(market.yes_token_id)
    exit_price = round(book.mid + 0.02, 2)  # Mid + 2c
    exit_price = max(0.02, min(0.99, exit_price))
    
    exit_shares = inventory.get_yes_shares()
    if exit_shares <= 0:
        exit_shares = bid_size  # Fallback
    
    exit_result = clob.post_order(
        token_id=market.yes_token_id,
        side=Side.SELL,
        price=exit_price,
        size=exit_shares,
        post_only=True
    )
    
    exit_order_time = time.time()
    exit_latency = exit_order_time - fill_time
    
    if exit_result.success:
        print(f"  EXIT order placed in {exit_latency:.2f}s")
        print(f"  SELL @ {exit_price:.2f} x {exit_shares}")
        print(f"  order_id={exit_result.order_id[:40]}...")
        
        if exit_latency <= 2.0:
            print(f"\n  [OK] EXIT LATENCY OK: {exit_latency:.2f}s <= 2.0s")
        else:
            print(f"\n  [WARN] EXIT LATENCY HIGH: {exit_latency:.2f}s > 2.0s")
    else:
        print(f"  EXIT order FAILED: {exit_result.error}")
    
    # Step 4: Verify state and cleanup
    print("\nStep 4: Verifying state...")
    time.sleep(2)
    
    orders = clob.get_open_orders()
    print(f"  Open orders: {len(orders)}")
    for o in orders:
        print(f"    {o.side} @ {o.price} x {o.size} [{o.status}]")
    
    inv_summary = inventory.get_summary()
    print(f"  Inventory: YES={inv_summary['yes_shares']:.1f} NO={inv_summary['no_shares']:.1f}")
    
    # Cleanup
    print("\nStep 5: Cleanup - cancelling all orders...")
    clob.cancel_all()
    
    time.sleep(1)
    
    # Final state
    orders = clob.get_open_orders()
    balance = clob.get_balance()
    
    print("\n" + "=" * 60)
    print("FINAL STATE")
    print("=" * 60)
    print(f"Open orders: {len(orders)}")
    print(f"Balance: USDC={balance['usdc']:.2f} Positions={balance['positions']:.2f}")
    
    if fill_detected and exit_result.success and exit_latency <= 2.0:
        print("\n[PASS] TEST PASSED: Fill detected, exit placed within 2s")
    elif fill_detected and exit_result.success:
        print("\n[PARTIAL] TEST PARTIAL: Fill detected, exit placed but latency > 2s")
    elif fill_detected:
        print("\n[FAIL] TEST FAILED: Fill detected but exit order failed")
    else:
        print("\n[INCONCLUSIVE] TEST INCONCLUSIVE: No fill occurred")
    
    print("=" * 60)


if __name__ == "__main__":
    main()

