"""Check status of dust positions"""

import sys
import os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from mm_bot.config import Config, RunMode
from mm_bot.clob import ClobWrapper
import requests

config = Config.from_env("pm_api_config.json")
config.mode = RunMode.LIVE
clob = ClobWrapper(config)

# Get all positions
r = requests.get(
    "https://data-api.polymarket.com/positions",
    params={"user": config.api.proxy_address},
    timeout=10
)
positions = r.json()

print("=" * 60)
print("POSITION STATUS")
print("=" * 60)

for p in positions:
    size = float(p.get("size", 0))
    if size < 0.01:
        continue
    
    token = p["asset"]
    avg = p.get("avgPrice", 0)
    
    print(f"\nToken: {token[:50]}...")
    print(f"  Size: {size:.2f} shares @ ${avg}")
    
    # Try to get book
    try:
        book = clob.get_order_book(token)
        if book and (book.best_bid > 0.01 or book.best_ask < 0.99):
            print(f"  Book: bid=${book.best_bid:.2f} ask=${book.best_ask:.2f}")
            print("  Status: ACTIVE (market still open)")
            current_value = size * book.best_bid
            print(f"  Current Value: ${current_value:.2f}")
        else:
            print("  Status: NO BOOK (market ended, waiting for settlement)")
    except Exception as e:
        print(f"  Status: ERROR ({e})")

print()
print("=" * 60)
bal = clob.get_balance()
print(f"Total USDC: ${bal['usdc']:.2f}")
print(f"Total Positions Value: ${bal['positions']:.2f}")
print("=" * 60)


