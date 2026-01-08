"""
Scan BTC 15-minute markets - these have tighter spreads and faster action.
"""

import json
import requests
import time

GAMMA_API = "https://gamma-api.polymarket.com"
CLOB_API = "https://clob.polymarket.com"

session = requests.Session()

# Search for BTC updown markets
print("Searching for BTC 15-min Up/Down markets...")

resp = session.get(f"{GAMMA_API}/markets", params={
    "active": "true",
    "limit": "100",
}, timeout=10)
markets = resp.json()

btc_markets = [m for m in markets if "btc" in m.get("slug", "").lower() and "updown" in m.get("slug", "").lower()]

print(f"Found {len(btc_markets)} BTC updown markets\n")

if not btc_markets:
    # Try searching differently
    print("Trying broader search...")
    btc_markets = [m for m in markets if "btc" in m.get("slug", "").lower()]
    print(f"Found {len(btc_markets)} BTC markets")

for m in btc_markets[:10]:
    print(f"\n{m.get('slug')}")
    print(f"  Question: {m.get('question', '')[:60]}")
    
    outcomes = m.get("outcomes", [])
    if isinstance(outcomes, str):
        outcomes = json.loads(outcomes)
    
    tokens = m.get("clobTokenIds", [])
    if isinstance(tokens, str):
        tokens = json.loads(tokens)
    
    print(f"  Outcomes: {outcomes}")
    
    for i, (outcome, token) in enumerate(zip(outcomes, tokens)):
        try:
            resp = session.get(f"{CLOB_API}/book", params={"token_id": token}, timeout=5)
            book = resp.json()
            
            bids = book.get("bids", [])
            asks = book.get("asks", [])
            
            if bids and asks:
                best_bid = float(bids[0]["price"])
                best_ask = float(asks[0]["price"])
                bid_size = float(bids[0]["size"])
                ask_size = float(asks[0]["size"])
                spread = (best_ask - best_bid) * 100
                
                print(f"  {outcome}: bid={best_bid:.4f} (${bid_size:.0f}) | ask={best_ask:.4f} (${ask_size:.0f}) | spread={spread:.2f}c")
            else:
                print(f"  {outcome}: no orderbook")
        except Exception as e:
            print(f"  {outcome}: error - {e}")
    
    # Check for arb
    if len(tokens) >= 2:
        try:
            asks = []
            for token in tokens:
                resp = session.get(f"{CLOB_API}/book", params={"token_id": token}, timeout=5)
                book = resp.json()
                ask_list = book.get("asks", [])
                if ask_list:
                    asks.append(float(ask_list[0]["price"]))
            
            if len(asks) >= 2:
                ask_sum = sum(asks)
                print(f"  ASK SUM: {ask_sum:.4f} (arb if < 1.0)")
                if ask_sum < 1.0:
                    print(f"  *** ARB OPPORTUNITY: {(1-ask_sum)*100:.2f}c edge ***")
        except:
            pass

# Also look at all markets for any with reasonable spreads
print("\n\n" + "="*60)
print("Scanning ALL markets for reasonable spreads...")
print("="*60)

for m in markets[:200]:
    try:
        outcomes = m.get("outcomes", [])
        if isinstance(outcomes, str):
            outcomes = json.loads(outcomes)
        
        tokens = m.get("clobTokenIds", [])
        if isinstance(tokens, str):
            tokens = json.loads(tokens)
        
        if len(tokens) < 2:
            continue
        
        # Fetch both books
        books = []
        for token in tokens:
            resp = session.get(f"{CLOB_API}/book", params={"token_id": token}, timeout=5)
            books.append(resp.json())
        
        # Check spreads on each
        for i, (outcome, book) in enumerate(zip(outcomes, books)):
            bids = book.get("bids", [])
            asks = book.get("asks", [])
            
            if not bids or not asks:
                continue
            
            best_bid = float(bids[0]["price"])
            best_ask = float(asks[0]["price"])
            
            # Skip extreme prices
            if best_bid < 0.10 or best_ask > 0.90:
                continue
            
            spread = (best_ask - best_bid) * 100
            
            if spread < 3.0:  # Very tight spread
                print(f"\nTight spread: {m.get('slug')[:50]}")
                print(f"  {outcome}: bid={best_bid:.4f} ask={best_ask:.4f} spread={spread:.2f}c")
        
        time.sleep(0.02)
        
    except Exception as e:
        pass

print("\nDone scanning.")

