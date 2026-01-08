#!/usr/bin/env python3
"""Compare raw API vs CLOBClient for the same token."""

import requests
from bot.clob import CLOBClient

token_id = "197393853864312420258790826286091037087831854818728181927073605339403890785273"

print(f"Token ID: {token_id}")
print()

# Raw API call
print("=== RAW API CALL ===")
url = f"https://clob.polymarket.com/book?token_id={token_id}"
r = requests.get(url, timeout=15)
print(f"Status: {r.status_code}")
if r.status_code == 200:
    d = r.json()
    print(f"Asks (raw, first 5): {d.get('asks', [])[:5]}")
    print(f"Bids (raw, first 5): {d.get('bids', [])[:5]}")
else:
    print(f"Error: {r.text}")

print()

# CLOBClient
print("=== CLOBClient ===")
clob = CLOBClient()
book = clob.get_book(token_id)
print(f"Asks (parsed, first 5): {book.asks[:5]}")
print(f"Bids (parsed, first 5): {book.bids[:5]}")





