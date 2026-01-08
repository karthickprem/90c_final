"""Check real balance from Polymarket"""

import json
import requests

with open("pm_api_config.json") as f:
    config = json.load(f)

proxy = config["proxy_address"]
print(f"Proxy wallet: {proxy}")

# Try different balance endpoints
endpoints = [
    f"https://data-api.polymarket.com/value?user={proxy}",
    f"https://gamma-api.polymarket.com/users/{proxy}",
    f"https://clob.polymarket.com/balance?address={proxy}",
]

for url in endpoints:
    print(f"\nTrying: {url[:60]}...")
    try:
        r = requests.get(url, timeout=10)
        print(f"  Status: {r.status_code}")
        if r.status_code == 200:
            print(f"  Response: {r.text[:500]}")
    except Exception as e:
        print(f"  Error: {e}")

# Also check positions
print(f"\n\nPositions:")
try:
    r = requests.get(f"https://data-api.polymarket.com/positions?user={proxy}", timeout=10)
    print(f"  Status: {r.status_code}")
    if r.status_code == 200:
        data = r.json()
        if data:
            for pos in data[:5]:
                print(f"  - {pos.get('outcome')}: {pos.get('size')} @ {pos.get('avgPrice')}")
        else:
            print("  No positions")
except Exception as e:
    print(f"  Error: {e}")

