#!/usr/bin/env python3
"""Dump raw orderbook for the buckets in question."""

import requests
import json
from bot.gamma import GammaClient

g = GammaClient()
m = g.discover_bucket_markets(locations=['london'])

# Get Jan 4 buckets
jan4 = [x for x in m if x.target_date.day == 4 and not x.is_tail_bucket]
jan4.sort(key=lambda x: x.tmin_f)

print("=== JAN 4 LONDON BUCKETS ===")
print()

for mkt in jan4:
    token_id = mkt.yes_token_id
    print(f"Question: {mkt.question}")
    print(f"Parsed: {mkt.tmin_f:.2f}-{mkt.tmax_f:.2f}F (width: {mkt.tmax_f - mkt.tmin_f:.2f}F)")
    print(f"Original unit: {mkt.temp_unit}")
    print(f"Token: {token_id}")
    
    # Raw API
    url = f"https://clob.polymarket.com/book?token_id={token_id}"
    r = requests.get(url, timeout=15)
    d = r.json()
    
    asks = d.get("asks", [])[:5]
    bids = d.get("bids", [])[:5]
    
    print("First 5 asks:")
    for a in asks:
        print(f"  price={a['price']} size={a['size']}")
    if not asks:
        print("  (empty)")
    
    print("First 5 bids:")
    for b in bids:
        print(f"  price={b['price']} size={b['size']}")
    if not bids:
        print("  (empty)")
    
    print()
    print("-" * 60)
    print()





