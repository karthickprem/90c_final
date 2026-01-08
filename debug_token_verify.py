#!/usr/bin/env python3
"""Verify token IDs are consistent."""

import requests
from bot.gamma import GammaClient

g = GammaClient()
m = g.discover_bucket_markets(locations=['london'])

# Get the 36-37F bucket for Jan 3
jan3_36 = [x for x in m if x.target_date.day == 3 and x.tmin_f > 35 and x.tmin_f < 38 and not x.is_tail_bucket]

bucket = jan3_36[0]
yes_token = bucket.yes_token_id
no_token = bucket.no_token_id

print(f"Question: {bucket.question}")
print(f"YES Token (full): {yes_token}")
print(f"NO Token (full): {no_token}")
print(f"YES Token length: {len(yes_token)}")
print(f"NO Token length: {len(no_token)}")
print()

# Check YES token orderbook
print("=== YES TOKEN ORDERBOOK ===")
url = f"https://clob.polymarket.com/book?token_id={yes_token}"
r = requests.get(url, timeout=15)
print(f"Status: {r.status_code}")
if r.status_code == 200:
    d = r.json()
    asks = d.get('asks', [])[:3]
    print(f"First 3 asks: {asks}")
    
# Check NO token orderbook  
print()
print("=== NO TOKEN ORDERBOOK ===")
url = f"https://clob.polymarket.com/book?token_id={no_token}"
r = requests.get(url, timeout=15)
print(f"Status: {r.status_code}")
if r.status_code == 200:
    d = r.json()
    asks = d.get('asks', [])[:3]
    print(f"First 3 asks: {asks}")





