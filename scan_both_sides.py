"""
Scan BOTH outcomes for each market to find actual trading opportunities.

The key insight: if YES is 0.01/0.99, then NO is ~0.01/0.99 on the other side.
We need to find markets where:
1. There's actual two-sided trading (spreads < 5c)
2. Or one side is cheap enough to buy for probable settlement
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
        "limit": "200"
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

# Find markets with tight spreads (actual trading)
tight_spread_markets = []

print("Scanning for markets with tight spreads (< 5c)...\n")

for m in markets[:100]:
    try:
        slug = m.get("slug", "")
        volume = float(m.get("volume", 0) or 0)
        
        outcomes = m.get("outcomes", [])
        if isinstance(outcomes, str):
            outcomes = json.loads(outcomes)
        
        tokens = m.get("clobTokenIds", [])
        if isinstance(tokens, str):
            tokens = json.loads(tokens)
        
        if len(tokens) != len(outcomes):
            continue
        
        for i, (outcome, token) in enumerate(zip(outcomes, tokens)):
            book = fetch_book(token)
            if not book:
                continue
            
            bids = book.get("bids", [])
            asks = book.get("asks", [])
            
            if not bids or not asks:
                continue
            
            best_bid = float(bids[0]["price"])
            best_ask = float(asks[0]["price"])
            spread_cents = (best_ask - best_bid) * 100
            
            # Only show tight spreads (< 5c) - these have real trading
            if spread_cents < 5.0 and best_bid > 0.05 and best_ask < 0.95:
                tight_spread_markets.append({
                    "slug": slug,
                    "outcome": outcome,
                    "bid": best_bid,
                    "ask": best_ask,
                    "spread": spread_cents,
                    "volume": volume,
                    "mid": (best_bid + best_ask) / 2,
                })
        
        time.sleep(0.05)
        
    except Exception as e:
        pass

print(f"{'Spread':<8} {'Bid':<8} {'Ask':<8} {'Mid':<8} {'Volume':<12} {'Outcome':<15} {'Market'}")
print("-" * 110)

# Sort by spread
tight_spread_markets.sort(key=lambda x: x["spread"])

for m in tight_spread_markets[:30]:
    print(f"{m['spread']:>6.2f}c {m['bid']:>6.4f} {m['ask']:>6.4f} {m['mid']:>6.2%} "
          f"${m['volume']:>10,.0f} {str(m['outcome'])[:15]:<15} {m['slug'][:40]}")

print(f"\nFound {len(tight_spread_markets)} outcomes with tight spreads (< 5c)")

# Now look at the SUM of asks for multi-outcome markets
print("\n\nLooking for multi-outcome arb (sum of asks < 1)...")
print("-" * 80)

for m in markets[:50]:
    try:
        outcomes = m.get("outcomes", [])
        if isinstance(outcomes, str):
            outcomes = json.loads(outcomes)
        
        tokens = m.get("clobTokenIds", [])
        if isinstance(tokens, str):
            tokens = json.loads(tokens)
        
        if len(tokens) < 2:
            continue
        
        asks = []
        for token in tokens:
            book = fetch_book(token)
            if book:
                ask_list = book.get("asks", [])
                if ask_list:
                    asks.append(float(ask_list[0]["price"]))
                else:
                    asks.append(1.0)
            else:
                asks.append(1.0)
        
        ask_sum = sum(asks)
        
        # Arb exists if sum < 1
        if ask_sum < 1.0:
            edge = (1 - ask_sum) * 100
            print(f"ARB! {m['slug'][:50]}")
            print(f"  Asks: {asks} = {ask_sum:.4f}")
            print(f"  Edge: {edge:.2f}c per $1")
            print()
        
        time.sleep(0.05)
        
    except Exception as e:
        pass

print("Scan complete.")

