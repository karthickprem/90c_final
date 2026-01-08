"""Check trades API format"""
import os
os.environ["LIVE"] = "1"

import requests
from mm_bot.config import Config

c = Config.from_env()

r = requests.get(
    "https://data-api.polymarket.com/trades",
    params={"user": c.api.proxy_address, "limit": 5},
    timeout=10
)

print("Trades API response:")
if r.ok:
    trades = r.json()
    print(f"Got {len(trades)} trades")
    for i, t in enumerate(trades[:3]):
        print(f"\nTrade {i+1}:")
        print(f"  Keys: {list(t.keys())}")
        for key in ["id", "tradeId", "trade_id", "side", "price", "size", "asset", "timestamp"]:
            val = t.get(key, "MISSING")
            print(f"  {key}: {val}")
else:
    print(f"Error: {r.status_code}")
    print(r.text)

