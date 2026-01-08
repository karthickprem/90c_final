#!/usr/bin/env python3
"""Step by step trace of get_book()."""

import requests
from bot.gamma import GammaClient

# Get token
g = GammaClient()
m = g.discover_bucket_markets(locations=['london'])
jan3_36 = [x for x in m if x.target_date.day == 3 and x.tmin_f > 35 and x.tmin_f < 38 and not x.is_tail_bucket][0]
token_id = jan3_36.yes_token_id

print(f"Token ID: {token_id}")
print()

# Now step through get_book() manually
import yaml
with open("bot/config.yaml") as f:
    config = yaml.safe_load(f)

base_url = config.get("clob_api_url", "https://clob.polymarket.com")
print(f"Base URL: {base_url}")

# Make request
session = requests.Session()
url = f"{base_url}/book"
params = {"token_id": token_id}
print(f"Request URL: {url}")
print(f"Params: {params}")

response = session.get(url, params=params, timeout=10)
print(f"Response status: {response.status_code}")
print(f"Actual URL: {response.url}")

data = response.json()
print(f"Response keys: {list(data.keys())}")
print(f"Asset ID in response: {data.get('asset_id', 'N/A')}")

raw_asks = data.get("asks") or []
print(f"\nRaw asks (first 5):")
for a in raw_asks[:5]:
    print(f"  {a}")

# Parse asks like get_book does
from bot.clob import OrderBookLevel
asks = []
for level in raw_asks:
    price = float(level.get("price", 0))
    size = float(level.get("size", 0))
    asks.append(OrderBookLevel(price=price, size=size))

print(f"\nParsed asks before sort (first 5):")
for a in asks[:5]:
    print(f"  price={a.price:.4f} size={a.size:.2f}")

# Sort ascending
asks.sort(key=lambda x: x.price)
print(f"\nParsed asks after sort (first 5):")
for a in asks[:5]:
    print(f"  price={a.price:.4f} size={a.size:.2f}")





