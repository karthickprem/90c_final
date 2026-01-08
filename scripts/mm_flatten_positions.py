"""
Flatten Positions Script
========================
Safely unwind BTC 15m positions only.
Does NOT touch other markets (Egypt, etc.) unless MM_CLOSE_ALL=1.
"""

import sys
import os
import time
import argparse

# Force unbuffered output
import builtins
_print = builtins.print
def print(*args, **kwargs):
    kwargs['flush'] = True
    _print(*args, **kwargs)

# Add parent to path
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from mm_bot.config import Config, RunMode
from mm_bot.clob import ClobWrapper
from mm_bot.market import MarketResolver


def main():
    parser = argparse.ArgumentParser(description="Flatten BTC 15m positions safely")
    parser.add_argument("--config", default="pm_api_config.json", help="Config file")
    parser.add_argument("--dry-run", action="store_true", help="Don't actually place orders")
    parser.add_argument("--close-all", action="store_true", help="Close ALL positions")
    args = parser.parse_args()
    
    # Check env
    if os.environ.get("LIVE") != "1":
        print("[ERROR] This script requires LIVE=1 to run (safety check)")
        return
    
    if args.close_all or os.environ.get("MM_CLOSE_ALL") == "1":
        print("[WARN] MM_CLOSE_ALL mode - will close ALL positions")
        close_all = True
    else:
        close_all = False
    
    # Load config
    config = Config.from_env(args.config)
    config.mode = RunMode.LIVE
    
    clob = ClobWrapper(config)
    resolver = MarketResolver(config)
    
    print("=" * 50)
    print("FLATTEN POSITIONS SCRIPT")
    print("=" * 50)
    
    # Get current market tokens
    market = resolver.resolve_market()
    if not market:
        print("[ERROR] Could not resolve current BTC 15m market")
        return
    
    btc_tokens = {market.yes_token_id, market.no_token_id}
    print(f"[INFO] Current BTC 15m tokens:")
    print(f"       YES: {market.yes_token_id[:30]}...")
    print(f"       NO:  {market.no_token_id[:30]}...")
    
    # Cancel all open orders first
    print("[INFO] Cancelling all open orders...")
    clob.cancel_all()
    time.sleep(1)
    
    # Get balance and positions
    balance = clob.get_balance()
    print(f"[INFO] USDC Balance: ${balance['usdc']:.2f}")
    print(f"[INFO] Position Value: ${balance['positions']:.2f}")
    
    # Get open orders to check for fills
    open_orders = clob.get_open_orders()
    print(f"[INFO] Open orders after cancel: {len(open_orders)}")
    
    # For each token with inventory, attempt to sell
    # Note: We need to infer inventory from orders' matched size
    inventory = {}
    for order in open_orders:
        if order.size_matched > 0:
            token = order.token_id
            if token not in inventory:
                inventory[token] = 0
            if order.side == "BUY":
                inventory[token] += order.size_matched
            else:
                inventory[token] -= order.size_matched
    
    print(f"[INFO] Detected inventory from orders: {len(inventory)} tokens")
    
    for token_id, shares in inventory.items():
        if shares <= 0:
            continue
        
        # Check if this is a BTC 15m token
        if not close_all and token_id not in btc_tokens:
            print(f"[SKIP] Token {token_id[:30]}... is not BTC 15m (use --close-all to include)")
            continue
        
        label = "YES" if token_id == market.yes_token_id else "NO" if token_id == market.no_token_id else "OTHER"
        print(f"[FLATTEN] {label} token: {shares:.1f} shares")
        
        if args.dry_run:
            print(f"         [DRY RUN] Would place SELL order")
            continue
        
        # Get book
        book = clob.get_order_book(token_id)
        if not book or book.best_bid <= 0:
            print(f"         [ERROR] No valid book for {token_id[:30]}...")
            continue
        
        # Place aggressive sell
        exit_price = round(book.best_bid + 0.01, 2)
        exit_price = max(0.01, min(0.99, exit_price))
        
        print(f"         [SELL] @ {exit_price:.2f} x {shares:.1f} (best_bid={book.best_bid:.2f})")
        
        result = clob.post_order(
            token_id=token_id,
            side="SELL",
            price=exit_price,
            size=shares,
            post_only=True
        )
        
        if result.success:
            print(f"         [OK] Order placed: {result.order_id[:30]}...")
        else:
            print(f"         [FAIL] {result.error}")
    
    # Wait and check final state
    if not args.dry_run:
        print("[INFO] Waiting 3s for settlement...")
        time.sleep(3)
        
        final_orders = clob.get_open_orders()
        final_balance = clob.get_balance()
        
        print("=" * 50)
        print("FINAL STATE")
        print("=" * 50)
        print(f"Open orders: {len(final_orders)}")
        print(f"USDC: ${final_balance['usdc']:.2f}")
        print(f"Positions: ${final_balance['positions']:.2f}")
    
    print("[DONE] Flatten complete")


if __name__ == "__main__":
    main()

