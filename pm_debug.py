"""
Debug: Print ALL market data from Polymarket APIs
"""

import requests
import json
import time

GAMMA_API = "https://gamma-api.polymarket.com"
CLOB_API = "https://clob.polymarket.com"

session = requests.Session()

def main():
    # Current window
    ts = int(time.time())
    start = ts - (ts % 900)
    slug = f"btc-updown-15m-{start}"
    
    print(f"Current window: {slug}", flush=True)
    print("=" * 70, flush=True)
    
    # Get full market data from Gamma
    print("\n1. GAMMA API /markets response:", flush=True)
    r = session.get(f"{GAMMA_API}/markets", params={"slug": slug}, timeout=10)
    markets = r.json()
    
    if markets:
        m = markets[0]
        # Print all keys
        print(f"   Keys: {list(m.keys())}", flush=True)
        
        # Print relevant fields
        print(f"\n   outcomePrices: {m.get('outcomePrices')}", flush=True)
        print(f"   bestBid: {m.get('bestBid')}", flush=True)
        print(f"   bestAsk: {m.get('bestAsk')}", flush=True)
        print(f"   lastTradePrice: {m.get('lastTradePrice')}", flush=True)
        print(f"   volume: {m.get('volume')}", flush=True)
        print(f"   liquidity: {m.get('liquidity')}", flush=True)
        
        # Get tokens
        tokens = m.get("clobTokenIds", [])
        outcomes = m.get("outcomes", [])
        if isinstance(tokens, str):
            tokens = json.loads(tokens)
        if isinstance(outcomes, str):
            outcomes = json.loads(outcomes)
        
        # Get orderbook for each
        for o, t in zip(outcomes, tokens):
            print(f"\n2. CLOB ORDERBOOK for {o}:", flush=True)
            r = session.get(f"{CLOB_API}/book", params={"token_id": t}, timeout=5)
            book = r.json()
            
            bids = book.get("bids", [])[:5]
            asks = book.get("asks", [])[:5]
            
            print(f"   BIDS (top 5):", flush=True)
            for b in bids:
                print(f"      {float(b['price']):.4f} x {float(b['size']):.2f}", flush=True)
            
            print(f"   ASKS (top 5):", flush=True)
            for a in asks:
                print(f"      {float(a['price']):.4f} x {float(a['size']):.2f}", flush=True)
        
        # Try price endpoint
        print(f"\n3. CLOB /price endpoint:", flush=True)
        for o, t in zip(outcomes, tokens):
            try:
                r = session.get(f"{CLOB_API}/price", params={"token_id": t, "side": "BUY"}, timeout=5)
                print(f"   {o} BUY: {r.json()}", flush=True)
            except Exception as e:
                print(f"   {o} BUY: Error - {e}", flush=True)
            
            try:
                r = session.get(f"{CLOB_API}/price", params={"token_id": t, "side": "SELL"}, timeout=5)
                print(f"   {o} SELL: {r.json()}", flush=True)
            except Exception as e:
                print(f"   {o} SELL: Error - {e}", flush=True)
        
        # Try midpoint endpoint
        print(f"\n4. CLOB /midpoint endpoint:", flush=True)
        for o, t in zip(outcomes, tokens):
            try:
                r = session.get(f"{CLOB_API}/midpoint", params={"token_id": t}, timeout=5)
                print(f"   {o}: {r.json()}", flush=True)
            except Exception as e:
                print(f"   {o}: Error - {e}", flush=True)

if __name__ == "__main__":
    main()

