"""
Check Trades API Format
=======================

Verifies that:
1. Trades API is accessible
2. transactionHash field is present
3. All required fields exist

This must PASS before LIVE trading.
"""

import os
import sys
os.environ["LIVE"] = "1"

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import requests
from mm_bot.config import Config

print("=" * 60)
print("  TRADES API FORMAT CHECK")
print("=" * 60)

config = Config.from_env()
proxy = config.api.proxy_address

print(f"\nProxy address: {proxy[:20]}...")

r = requests.get(
    "https://data-api.polymarket.com/trades",
    params={"user": proxy, "limit": 5},
    timeout=10
)

print(f"Status: {r.status_code}")

if r.status_code != 200:
    print(f"ERROR: API returned {r.status_code}")
    print(r.text[:200])
    sys.exit(1)

trades = r.json()
print(f"Trades returned: {len(trades)}")

if not trades:
    print("\nNo trades found - cannot verify format")
    print("RESULT: SKIP (no trades to check)")
    sys.exit(2)

# Check required fields
REQUIRED_FIELDS = ["transactionHash", "asset", "side", "size", "price", "timestamp"]
OPTIONAL_FIELDS = ["orderId", "maker", "fee", "rebate"]

print("\n" + "-" * 40)
all_valid = True

for i, trade in enumerate(trades[:3]):
    print(f"\nTrade {i+1}:")
    
    # Check required
    for field in REQUIRED_FIELDS:
        val = trade.get(field)
        if val is None or val == "":
            print(f"  [FAIL] {field}: MISSING")
            all_valid = False
        else:
            if field == "transactionHash":
                # Show first 20 chars
                print(f"  [OK] {field}: {str(val)[:40]}...")
            else:
                print(f"  [OK] {field}: {val}")
    
    # Check optional
    for field in OPTIONAL_FIELDS:
        val = trade.get(field)
        status = "present" if val is not None else "missing"
        print(f"  [INFO] {field}: {status}")

print("\n" + "=" * 60)

if all_valid:
    print("RESULT: PASS - All required fields present")
    print("transactionHash is available for dedupe")
    sys.exit(0)
else:
    print("RESULT: FAIL - Missing required fields")
    print("DO NOT RUN LIVE")
    sys.exit(1)

