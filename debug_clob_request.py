#!/usr/bin/env python3
"""Debug what request CLOBClient is actually making."""

import requests

token_id = '53991946850993283973244839140451693673829533119752949187307306094539848989166'

# Exactly how CLOBClient does it
from bot.clob import CLOBClient
clob = CLOBClient()

print("CLOBClient config:")
print(f"  base_url: {clob.base_url}")
print(f"  use_depth: {clob.use_depth}")
print()

# Trace the actual request
print(f"Calling: {clob.base_url}/book?token_id={token_id}")
print()

# Raw request using clob's session
resp = clob.session.get(f"{clob.base_url}/book", params={"token_id": token_id}, timeout=10)
print(f"Status: {resp.status_code}")
print(f"Actual URL: {resp.url}")
data = resp.json()
print(f"First 3 asks from raw response:")
for a in data.get("asks", [])[:3]:
    print(f"  {a}")
print()

# Now call get_book
print("Calling clob.get_book():")
book = clob.get_book(token_id)
print(f"First 3 asks from get_book():")
for a in book.asks[:3]:
    print(f"  {a}")





