"""
LIVE DATA TEST - Prove we can read Polymarket data
"""

import requests
import json
import time
import sys

GAMMA_API = "https://gamma-api.polymarket.com"
CLOB_API = "https://clob.polymarket.com"

session = requests.Session()

def get_btc_price():
    """Get current BTC price from CoinGecko."""
    try:
        r = session.get("https://api.coingecko.com/api/v3/simple/price",
                       params={"ids": "bitcoin", "vs_currencies": "usd"}, timeout=10)
        return r.json().get("bitcoin", {}).get("usd", 0)
    except Exception as e:
        return f"ERROR: {e}"

def get_current_window():
    """Get current 15-min window info."""
    ts = int(time.time())
    start = ts - (ts % 900)
    end = start + 900
    slug = f"btc-updown-15m-{start}"
    secs_left = end - ts
    return slug, start, end, secs_left

def get_market_from_gamma(slug):
    """Fetch market data from Gamma API."""
    try:
        r = session.get(f"{GAMMA_API}/markets", params={"slug": slug}, timeout=10)
        return r.json()
    except Exception as e:
        return f"ERROR: {e}"

def get_orderbook(token_id):
    """Fetch orderbook from CLOB API."""
    try:
        r = session.get(f"{CLOB_API}/book", params={"token_id": token_id}, timeout=5)
        return r.json()
    except Exception as e:
        return f"ERROR: {e}"

def main():
    print("=" * 70, flush=True)
    print("POLYMARKET LIVE DATA TEST", flush=True)
    print("=" * 70, flush=True)
    print("Press Ctrl+C to stop\n", flush=True)
    
    while True:
        try:
            print("\n" + "=" * 70, flush=True)
            print(f"TIMESTAMP: {time.strftime('%Y-%m-%d %H:%M:%S')}", flush=True)
            print("=" * 70, flush=True)
            
            # 1. BTC Price
            print("\n[1] BTC PRICE (CoinGecko):", flush=True)
            btc = get_btc_price()
            print(f"    ${btc:,.2f}" if isinstance(btc, (int, float)) else f"    {btc}", flush=True)
            
            # 2. Current Window
            print("\n[2] CURRENT 15-MIN WINDOW:", flush=True)
            slug, start, end, secs_left = get_current_window()
            print(f"    Slug: {slug}", flush=True)
            print(f"    Start: {start} ({time.strftime('%H:%M:%S', time.localtime(start))})", flush=True)
            print(f"    End: {end} ({time.strftime('%H:%M:%S', time.localtime(end))})", flush=True)
            print(f"    Seconds left: {secs_left}", flush=True)
            
            # 3. Market Data from Gamma
            print("\n[3] GAMMA API MARKET DATA:", flush=True)
            markets = get_market_from_gamma(slug)
            
            if isinstance(markets, str):
                print(f"    {markets}", flush=True)
            elif not markets:
                print("    No market found for this slug", flush=True)
            else:
                m = markets[0]
                print(f"    Question: {m.get('question', 'N/A')}", flush=True)
                print(f"    Condition ID: {m.get('conditionId', 'N/A')[:20]}...", flush=True)
                
                # Parse tokens
                tokens = m.get("clobTokenIds", [])
                outcomes = m.get("outcomes", [])
                if isinstance(tokens, str):
                    tokens = json.loads(tokens)
                if isinstance(outcomes, str):
                    outcomes = json.loads(outcomes)
                
                print(f"    Outcomes: {outcomes}", flush=True)
                print(f"    Token IDs:", flush=True)
                
                token_up = None
                token_down = None
                for o, t in zip(outcomes, tokens):
                    print(f"      {o}: {t[:20]}...", flush=True)
                    if str(o).lower() == "up":
                        token_up = t
                    elif str(o).lower() == "down":
                        token_down = t
                
                # 4. Orderbook for UP
                if token_up:
                    print("\n[4] ORDERBOOK - UP:", flush=True)
                    book_up = get_orderbook(token_up)
                    if isinstance(book_up, str):
                        print(f"    {book_up}", flush=True)
                    else:
                        bids = book_up.get("bids", [])[:5]
                        asks = book_up.get("asks", [])[:5]
                        
                        print("    BIDS (buyers):", flush=True)
                        if bids:
                            for b in bids:
                                print(f"      {float(b['price']):.4f} x {float(b['size']):.2f}", flush=True)
                        else:
                            print("      (empty)", flush=True)
                        
                        print("    ASKS (sellers):", flush=True)
                        if asks:
                            for a in asks:
                                print(f"      {float(a['price']):.4f} x {float(a['size']):.2f}", flush=True)
                        else:
                            print("      (empty)", flush=True)
                
                # 5. Orderbook for DOWN
                if token_down:
                    print("\n[5] ORDERBOOK - DOWN:", flush=True)
                    book_down = get_orderbook(token_down)
                    if isinstance(book_down, str):
                        print(f"    {book_down}", flush=True)
                    else:
                        bids = book_down.get("bids", [])[:5]
                        asks = book_down.get("asks", [])[:5]
                        
                        print("    BIDS (buyers):", flush=True)
                        if bids:
                            for b in bids:
                                print(f"      {float(b['price']):.4f} x {float(b['size']):.2f}", flush=True)
                        else:
                            print("      (empty)", flush=True)
                        
                        print("    ASKS (sellers):", flush=True)
                        if asks:
                            for a in asks:
                                print(f"      {float(a['price']):.4f} x {float(a['size']):.2f}", flush=True)
                        else:
                            print("      (empty)", flush=True)
            
            # 6. Summary
            print("\n[6] SUMMARY:", flush=True)
            if isinstance(markets, list) and markets:
                m = markets[0]
                tokens = m.get("clobTokenIds", [])
                outcomes = m.get("outcomes", [])
                if isinstance(tokens, str):
                    tokens = json.loads(tokens)
                if isinstance(outcomes, str):
                    outcomes = json.loads(outcomes)
                
                for o, t in zip(outcomes, tokens):
                    book = get_orderbook(t)
                    if isinstance(book, dict):
                        bids = book.get("bids", [])
                        asks = book.get("asks", [])
                        best_bid = float(bids[0]["price"]) if bids else 0
                        best_ask = float(asks[0]["price"]) if asks else 1
                        spread = (best_ask - best_bid) * 100
                        print(f"    {o}: Bid={best_bid:.4f} | Ask={best_ask:.4f} | Spread={spread:.1f}c", flush=True)
            
            print("\n--- Refreshing in 5 seconds ---", flush=True)
            time.sleep(5)
            
        except KeyboardInterrupt:
            print("\n\nStopped.", flush=True)
            break
        except Exception as e:
            print(f"\nERROR: {e}", flush=True)
            time.sleep(5)

if __name__ == "__main__":
    main()

