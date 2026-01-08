"""Check USDC cash balance"""

import json
import requests

with open("pm_api_config.json") as f:
    config = json.load(f)

proxy = config["proxy_address"]
print(f"Wallet: {proxy}")

# Get portfolio value
r = requests.get(f"https://data-api.polymarket.com/value?user={proxy}", timeout=10)
if r.status_code == 200:
    data = r.json()
    value = data[0].get("value", 0) if data else 0
    print(f"\nPortfolio value: ${value:.2f}")

# Get all positions
r = requests.get(f"https://data-api.polymarket.com/positions?user={proxy}", timeout=10)
if r.status_code == 200:
    positions = r.json()
    
    total_cost = 0
    print(f"\nOpen positions ({len(positions)}):")
    for p in positions:
        size = float(p.get("size", 0))
        avg = float(p.get("avgPrice", 0))
        cost = size * avg
        total_cost += cost
        cur = float(p.get("currentPrice", avg))
        val = size * cur
        print(f"  {p.get('outcome')}: {size:.2f} shares @ avg {avg*100:.0f}c (cost ${cost:.2f}, value ${val:.2f})")
    
    print(f"\nTotal cost in positions: ${total_cost:.2f}")
    print(f"Portfolio value: ${value:.2f}")
    print(f"Estimated USDC cash: ${value - total_cost:.2f}")
    
    # The real cash might be better estimated from the balance
    # But Polymarket doesn't expose this directly
    print(f"\n*** To find exact USDC: Check Polymarket UI ***")

