"""
Sell All Positions Script
==========================
Force-sells all positions using taker orders.
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
from mm_bot.clob import ClobWrapper
import requests


def main():
    # Check env
    if os.environ.get("LIVE") != "1":
        print("[ERROR] This script requires LIVE=1 to run (safety check)")
        return
    
    # Load config
    config = Config.from_env("pm_api_config.json")
    config.mode = RunMode.LIVE
    
    clob = ClobWrapper(config)
    
    print("=" * 50)
    print("SELL ALL POSITIONS")
    print("=" * 50)
    
    # Cancel all open orders first
    print("[INFO] Cancelling all open orders...")
    clob.cancel_all()
    time.sleep(1)
    
    # Get actual positions from REST
    r = requests.get(
        "https://data-api.polymarket.com/positions",
        params={"user": config.api.proxy_address},
        timeout=10
    )
    positions = r.json()
    
    sold_count = 0
    
    for p in positions:
        size = float(p.get("size", 0))
        if size < 0.01:
            continue
        
        token_id = p["asset"]
        avg_price = p.get("avgPrice", "N/A")
        print(f"\n[POSITION] {token_id[:40]}...")
        print(f"           {size:.2f} shares @ {avg_price}")
        
        # Get book to see current price
        try:
            book = clob.get_order_book(token_id)
        except Exception as e:
            print(f"           [SKIP] Could not get book: {e}")
            continue
        
        if not book or book.best_bid < 0.01:
            print(f"           [SKIP] No valid bid")
            continue
        
        sell_price = book.best_bid  # Sell at best bid to get filled
        print(f"           Book: bid={book.best_bid:.2f} ask={book.best_ask:.2f}")
        print(f"           SELL {size:.2f} @ {sell_price:.2f} (taker)...")
        
        try:
            result = clob.post_order(
                token_id=token_id,
                side="SELL",
                price=sell_price,
                size=size,
                post_only=False  # TAKER order to get filled
            )
            if result.success:
                print(f"           [OK] Order posted: {result.order_id[:30]}...")
                sold_count += 1
            else:
                print(f"           [FAILED] {result.error}")
        except Exception as e:
            print(f"           [ERROR] {e}")
        
        time.sleep(0.5)
    
    print()
    print("=" * 50)
    print(f"Attempted to sell {sold_count} positions")
    print("Waiting 3s for fills...")
    time.sleep(3)
    
    # Check final state
    print()
    r2 = requests.get(
        "https://data-api.polymarket.com/positions",
        params={"user": config.api.proxy_address},
        timeout=10
    )
    positions2 = r2.json()
    remaining = [p for p in positions2 if float(p.get("size", 0)) > 0]
    
    print(f"=== REMAINING POSITIONS: {len(remaining)} ===")
    for p in remaining:
        token = p["asset"][:40]
        size = float(p["size"])
        print(f"  {token}... {size:.2f} shares")
    
    bal = clob.get_balance()
    print(f"\nFinal USDC: ${bal['usdc']:.2f}")
    print(f"Final Position Value: ${bal['positions']:.2f}")
    print("=" * 50)


if __name__ == "__main__":
    main()


