"""
Find the actual active BTC 15-min markets using the events endpoint.
"""

import json
import requests
import time
from datetime import datetime

GAMMA_API = "https://gamma-api.polymarket.com"
CLOB_API = "https://clob.polymarket.com"

session = requests.Session()

# Try the events endpoint
print("Fetching events...")
resp = session.get(f"{GAMMA_API}/events", params={
    "active": "true",
    "limit": "100",
}, timeout=10)
events = resp.json()

print(f"Found {len(events)} events\n")

# Look for crypto/BTC related
crypto_events = []
for e in events:
    title = e.get("title", "").lower()
    slug = e.get("slug", "").lower()
    if any(word in title or word in slug for word in ["btc", "bitcoin", "crypto", "eth", "updown", "15m"]):
        crypto_events.append(e)

print(f"Found {len(crypto_events)} crypto-related events\n")

for e in crypto_events[:10]:
    print(f"\nEvent: {e.get('title')}")
    print(f"  Slug: {e.get('slug')}")
    
    markets = e.get("markets", [])
    print(f"  Markets: {len(markets)}")
    
    for m in markets[:3]:
        question = m.get("question", "")[:50]
        print(f"    - {question}")

# Also search for "up" "down" patterns
print("\n\n" + "="*60)
print("Looking for Up/Down markets...")
print("="*60)

updown_events = []
for e in events:
    title = e.get("title", "").lower()
    if "up" in title and "down" in title:
        updown_events.append(e)
    elif "updown" in title or "up/down" in title:
        updown_events.append(e)

print(f"Found {len(updown_events)} up/down events")

for e in updown_events[:5]:
    print(f"\n{e.get('title')}")
    print(f"  Slug: {e.get('slug')}")

# Try direct market search with different queries
print("\n\n" + "="*60)
print("Trying different search patterns...")
print("="*60)

search_terms = ["15m", "minute", "hourly", "bitcoin", "price", "sports", "game"]

for term in search_terms:
    resp = session.get(f"{GAMMA_API}/markets", params={
        "active": "true",
        "limit": "20",
    }, timeout=10)
    markets = resp.json()
    
    matches = [m for m in markets if term.lower() in m.get("slug", "").lower() or term.lower() in m.get("question", "").lower()]
    
    if matches:
        print(f"\n'{term}': Found {len(matches)} matches")
        for m in matches[:3]:
            print(f"  - {m.get('slug')[:50]}")

# Show the actual top volume markets with orderbook data
print("\n\n" + "="*60)
print("Top 20 markets by volume with orderbook data:")
print("="*60)

resp = session.get(f"{GAMMA_API}/markets", params={
    "active": "true",
    "closed": "false",
    "limit": "100",
}, timeout=10)
markets = resp.json()

# Sort by volume
markets.sort(key=lambda x: float(x.get("volume", 0) or 0), reverse=True)

count = 0
for m in markets:
    if count >= 20:
        break
    
    tokens = m.get("clobTokenIds", [])
    if isinstance(tokens, str):
        try:
            tokens = json.loads(tokens)
        except:
            continue
    
    if not tokens:
        continue
    
    # Fetch book for first token
    try:
        resp = session.get(f"{CLOB_API}/book", params={"token_id": tokens[0]}, timeout=5)
        book = resp.json()
        
        bids = book.get("bids", [])
        asks = book.get("asks", [])
        
        if bids and asks:
            volume = float(m.get("volume", 0) or 0)
            liquidity = float(m.get("liquidity", 0) or 0)
            best_bid = float(bids[0]["price"])
            best_ask = float(asks[0]["price"])
            spread = (best_ask - best_bid) * 100
            
            print(f"${volume:>12,.0f} | spread={spread:>6.2f}c | {m.get('slug')[:50]}")
            count += 1
    except:
        pass
    
    time.sleep(0.05)

