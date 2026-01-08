"""
MM Bot Live Smoke Test
======================
Posts ONE tiny postOnly bid far from mid and cancels it.
Safe smoke test to verify order placement works.

ONLY runs if LIVE=1 environment variable is set.
"""

import sys
import os
import time

# Add parent directory to path
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from mm_bot.config import Config, RunMode
from mm_bot.clob import ClobWrapper, Side
from mm_bot.market import MarketResolver


def main():
    # Check LIVE mode
    if os.environ.get("LIVE") != "1":
        print("=" * 60)
        print("LIVE SMOKE TEST SKIPPED")
        print("=" * 60)
        print("To run this test, set LIVE=1 in environment:")
        print()
        print("  Windows: set LIVE=1")
        print("  Then: python scripts/mm_live_smoke.py")
        print()
        print("This test will:")
        print("  1. Post ONE tiny bid at 0.05 (far from mid)")
        print("  2. Verify order exists")
        print("  3. Cancel the order")
        print("  4. Verify no orders remain")
        print("=" * 60)
        return
    
    print("=" * 60)
    print("LIVE SMOKE TEST")
    print("=" * 60)
    print("This will post ONE real order and cancel it.")
    print()
    
    # Load config
    config = Config.from_env("pm_api_config.json")
    config.mode = RunMode.LIVE
    
    # Validate config
    errors = config.validate()
    if errors:
        print("Config errors:")
        for err in errors:
            print(f"  - {err}")
        return
    
    print(f"Proxy: {config.api.proxy_address}")
    
    # Initialize components
    clob = ClobWrapper(config)
    market_resolver = MarketResolver(config)
    
    # Get current market
    market = market_resolver.resolve_market()
    if not market:
        print("ERROR: Could not resolve market")
        return
    
    print(f"Market: {market.slug}")
    print(f"YES token: {market.yes_token_id[:30]}...")
    print()
    
    # Get order book
    book = clob.get_order_book(market.yes_token_id)
    if not book:
        print("ERROR: Could not get order book")
        return
    
    print(f"Order book: bid={book.best_bid:.2f} ask={book.best_ask:.2f} mid={book.mid:.2f}")
    
    # Place a tiny bid far from mid (at 0.05 = 5 cents)
    # This should be a postOnly order that rests on the book
    test_price = 0.05  # Very low price, won't fill
    test_size = 5.0    # Minimum order size is 5 shares
    
    print()
    print(f"Step 1: Placing postOnly BID @ {test_price} x {test_size}")
    
    result = clob.post_order(
        token_id=market.yes_token_id,
        side=Side.BUY,
        price=test_price,
        size=test_size,
        post_only=True
    )
    
    if not result.success:
        print(f"  FAILED: {result.error}")
        if result.would_cross:
            print("  (Post-only rejected - would cross spread)")
        return
    
    print(f"  SUCCESS: order_id={result.order_id}")
    
    # Wait a moment
    time.sleep(1)
    
    # Verify order exists
    print()
    print("Step 2: Verifying order exists...")
    
    orders = clob.get_open_orders(market.yes_token_id)
    found = any(o.order_id == result.order_id for o in orders)
    
    if found:
        print(f"  Found {len(orders)} open order(s)")
        for o in orders:
            print(f"    - {o.side} @ {o.price} x {o.size} [{o.status}]")
    else:
        print("  WARNING: Order not found in open orders")
        # It may have already been cancelled or filled (unlikely at 5c)
    
    # Cancel the order
    print()
    print("Step 3: Cancelling order...")
    
    success = clob.cancel_order(result.order_id)
    if success:
        print("  Cancelled successfully")
    else:
        print("  WARNING: Cancel may have failed")
    
    # Verify no orders
    time.sleep(1)
    
    print()
    print("Step 4: Verifying no open orders...")
    
    orders = clob.get_open_orders(market.yes_token_id)
    remaining = [o for o in orders if o.order_id == result.order_id]
    
    if not remaining:
        print("  No test order remaining - PASS")
    else:
        print(f"  WARNING: Order still exists")
    
    print()
    print("=" * 60)
    print("SMOKE TEST COMPLETE")
    print("=" * 60)
    
    # Get balance
    balance = clob.get_balance()
    print(f"Balance: ${balance['usdc']:.2f} + ${balance['positions']:.2f} positions")


if __name__ == "__main__":
    main()

