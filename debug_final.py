#!/usr/bin/env python3
"""Final debug: compare raw API vs CLOBClient."""

import requests
from bot.gamma import GammaClient
from bot.clob import CLOBClient

g = GammaClient()
m = g.discover_bucket_markets(locations=['london'])

# Get the 36-37F bucket for Jan 3
jan3_36 = [x for x in m if x.target_date.day == 3 and x.tmin_f > 35 and x.tmin_f < 38 and not x.is_tail_bucket]

if not jan3_36:
    print("No matching bucket found")
    exit(1)

bucket = jan3_36[0]
token_id = bucket.yes_token_id

print(f"Question: {bucket.question}")
print(f"Token ID: {token_id}")
print()

# Raw API call
print("=== RAW API CALL ===")
url = f"https://clob.polymarket.com/book?token_id={token_id}"
r = requests.get(url, timeout=15)
print(f"Status: {r.status_code}")
if r.status_code == 200:
    d = r.json()
    asks = d.get('asks', [])[:5]
    bids = d.get('bids', [])[:5]
    print(f"Asks (first 5):")
    for a in asks:
        print(f"  price={a['price']} size={a['size']}")
    print(f"Bids (first 5):")
    for b in bids:
        print(f"  price={b['price']} size={b['size']}")
else:
    print(f"Error: {r.text}")

print()

# CLOBClient
print("=== CLOBClient ===")
clob = CLOBClient()
book = clob.get_book(token_id)
print(f"best_ask: {book.best_ask}")
print(f"Asks (first 5):")
for a in book.asks[:5]:
    print(f"  price={a.price:.4f} size={a.size:.2f}")





