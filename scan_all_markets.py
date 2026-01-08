"""
Quick scanner to see what's actually available on Polymarket right now.
No filters - just show raw data.
"""

import json
import requests
import time

GAMMA_API = "https://gamma-api.polymarket.com"
CLOB_API = "https://clob.polymarket.com"

session = requests.Session()

def fetch_markets():
    resp = session.get(f"{GAMMA_API}/markets", params={
        "active": "true", 
        "closed": "false",
        "limit": "100"
    }, timeout=10)
    return resp.json()

def fetch_book(token_id):
    try:
        resp = session.get(f"{CLOB_API}/book", params={"token_id": token_id}, timeout=5)
        return resp.json()
    except:
        return None

print("Fetching markets...")
markets = fetch_markets()
print(f"Got {len(markets)} markets\n")

# Sort by volume
markets.sort(key=lambda x: float(x.get("volume", 0) or 0), reverse=True)

print(f"{'Volume':<12} {'Liq':<10} {'Bid':<8} {'Ask':<8} {'Spread':<8} {'Market'}")
print("-" * 100)

count = 0
for m in markets[:50]:
    try:
        slug = m.get("slug", "")[:40]
        volume = float(m.get("volume", 0) or 0)
        liquidity = float(m.get("liquidity", 0) or 0)
        
        tokens = m.get("clobTokenIds", [])
        if isinstance(tokens, str):
            tokens = json.loads(tokens)
        
        if not tokens:
            continue
        
        # Get first token's book
        book = fetch_book(tokens[0])
        if not book:
            continue
        
        bids = book.get("bids", [])
        asks = book.get("asks", [])
        
        if not bids or not asks:
            continue
        
        best_bid = float(bids[0]["price"])
        best_ask = float(asks[0]["price"])
        spread = (best_ask - best_bid) * 100  # In cents
        
        print(f"${volume:>10,.0f} ${liquidity:>7,.0f} {best_bid:>6.4f} {best_ask:>6.4f} {spread:>6.2f}c {slug}")
        
        count += 1
        time.sleep(0.1)
        
    except Exception as e:
        pass

print(f"\nShowed {count} markets with orderbook data")

