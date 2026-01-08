"""Debug balance and allowance issues"""

import os
import sys
import requests

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from mm_bot.config import Config, RunMode
from mm_bot.clob import ClobWrapper

config = Config.from_env("pm_api_config.json")
config.mode = RunMode.LIVE
clob = ClobWrapper(config)

# Get balance details
b = clob.get_balance()
print("=== Balance ===")
print(f"USDC: ${b['usdc']:.4f}")
print(f"Positions MTM: ${b['positions']:.4f}")

# Get open orders
orders = clob.get_open_orders()
print(f"\nOpen orders: {len(orders)}")
for o in orders[:3]:
    print(f"  {o}")

# Get positions from REST
positions = requests.get(
    "https://data-api.polymarket.com/positions",
    params={"user": config.api.proxy_address},
    timeout=10
).json()
print(f"\nPositions from REST: {len(positions)}")
for p in positions[:5]:
    asset = p.get("asset", "")[:30]
    size = float(p.get("size", 0))
    if size > 0:
        print(f"  Token: {asset}... Size: {size:.2f}")

# Try to get CLOB client balance info
print(f"\n=== Debug Info ===")
print(f"Proxy address: {config.api.proxy_address}")
print(f"API key set: {bool(config.api.api_key)}")
print(f"Private key set: {bool(config.api.private_key)}")

# Check if there's locked balance
print("\n=== Order Value Check ===")
print("5 shares @ 0.46 = $2.30 required")
print(f"Available: ${b['usdc']:.4f}")
print(f"Should work: {b['usdc'] >= 2.30}")

