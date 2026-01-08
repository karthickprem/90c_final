"""
Check the orderbook depth for BTC 15m markets.
See if there are orders at reasonable prices deeper in the book.
"""

import requests
import json
import time

GAMMA_API = "https://gamma-api.polymarket.com"
CLOB_API = "https://clob.polymarket.com"

session = requests.Session()

# Get current window
ts = int(time.time())
window_start = ts - (ts % 900)
slug = f"btc-updown-15m-{window_start}"

print(f"Checking: {slug}")

# Fetch market
r = session.get(f"{GAMMA_API}/markets?slug={slug}", timeout=10)
markets = r.json()

if not markets:
    print("Market not found")
    exit()

market = markets[0]
print(f"Question: {market.get('question')}")

tokens = market.get("clobTokenIds", [])
if isinstance(tokens, str):
    tokens = json.loads(tokens)

outcomes = market.get("outcomes", [])
if isinstance(outcomes, str):
    outcomes = json.loads(outcomes)

for outcome, token in zip(outcomes, tokens):
    print(f"\n{'='*60}")
    print(f"OUTCOME: {outcome}")
    print(f"{'='*60}")
    
    r = session.get(f"{CLOB_API}/book", params={"token_id": token}, timeout=5)
    book = r.json()
    
    bids = book.get("bids", [])
    asks = book.get("asks", [])
    
    print(f"\nBIDS (top 15):")
    for i, level in enumerate(bids[:15]):
        price = float(level["price"])
        size = float(level["size"])
        print(f"  {i+1:2d}. {price:.4f} x ${size:>10,.0f}")
    
    print(f"\nASKS (top 15):")
    for i, level in enumerate(asks[:15]):
        price = float(level["price"])
        size = float(level["size"])
        print(f"  {i+1:2d}. {price:.4f} x ${size:>10,.0f}")

# Fetch current BTC price
r = session.get("https://api.coingecko.com/api/v3/simple/price",
                params={"ids": "bitcoin", "vs_currencies": "usd"}, timeout=10)
btc = r.json().get("bitcoin", {}).get("usd", 0)
print(f"\n\nCurrent BTC: ${btc:,.2f}")

# Calculate what price SHOULD be based on probability
# If we knew the opening price, we could calculate
print("\nProbability implications:")
print("If market is 50/50 (mid=0.50), it means:")
print("  - Market believes BTC has equal chance of ending up or down")
print("  - OR market is waiting for window to start")
print("  - OR no one is trading aggressively")

