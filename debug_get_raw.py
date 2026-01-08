#!/usr/bin/env python3
"""Trace exactly what _get returns."""

from bot.clob import CLOBClient

token_id = '53991946850993283973244839140451693673829533119752949187307306094539848989166'

clob = CLOBClient()

# Directly call _get to see raw response
data = clob._get("/book", params={"token_id": token_id})
print("Raw data from _get:")
print(f"  type: {type(data)}")
print(f"  keys: {list(data.keys())}")
print(f"  asks type: {type(data.get('asks'))}")
print(f"  asks length: {len(data.get('asks', []))}")
print(f"  First 3 asks:")
for a in data.get("asks", [])[:3]:
    print(f"    {a}")
print()

# Now trace get_book step by step
print("Tracing get_book:")
bids = []
for level in (data.get("bids") or []):
    bids.append((float(level.get("price", 0)), float(level.get("size", 0))))

asks = []
for level in (data.get("asks") or []):
    asks.append((float(level.get("price", 0)), float(level.get("size", 0))))

print(f"  Parsed asks before sort: {asks[:3]}")
asks.sort(key=lambda x: x[0])
print(f"  Parsed asks after sort: {asks[:3]}")





