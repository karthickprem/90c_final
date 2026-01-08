"""
LIVE DATA TEST V2 - Multiple BTC price sources
"""

import requests
import json
import time

GAMMA_API = "https://gamma-api.polymarket.com"
CLOB_API = "https://clob.polymarket.com"

session = requests.Session()

def get_btc_price():
    """Get BTC price from multiple sources."""
    # Try Binance first (most reliable)
    try:
        r = session.get("https://api.binance.com/api/v3/ticker/price", 
                       params={"symbol": "BTCUSDT"}, timeout=5)
        price = float(r.json()["price"])
        return price, "Binance"
    except:
        pass
    
    # Try CoinGecko
    try:
        r = session.get("https://api.coingecko.com/api/v3/simple/price",
                       params={"ids": "bitcoin", "vs_currencies": "usd"}, timeout=5)
        price = r.json().get("bitcoin", {}).get("usd", 0)
        if price > 0:
            return price, "CoinGecko"
    except:
        pass
    
    # Try Coinbase
    try:
        r = session.get("https://api.coinbase.com/v2/prices/BTC-USD/spot", timeout=5)
        price = float(r.json()["data"]["amount"])
        return price, "Coinbase"
    except:
        pass
    
    return 0, "None"

def get_window():
    ts = int(time.time())
    start = ts - (ts % 900)
    end = start + 900
    slug = f"btc-updown-15m-{start}"
    return slug, start, end, end - ts

def get_market(slug):
    try:
        r = session.get(f"{GAMMA_API}/markets", params={"slug": slug}, timeout=10)
        return r.json()
    except Exception as e:
        return None

def get_book(token_id):
    try:
        r = session.get(f"{CLOB_API}/book", params={"token_id": token_id}, timeout=5)
        return r.json()
    except:
        return None

def main():
    print("=" * 70, flush=True)
    print("LIVE DATA TEST V2", flush=True)
    print("=" * 70, flush=True)
    
    while True:
        try:
            print(f"\n{'='*70}", flush=True)
            print(f"TIME: {time.strftime('%H:%M:%S')}", flush=True)
            print("=" * 70, flush=True)
            
            # BTC Price
            btc, source = get_btc_price()
            print(f"\nBTC PRICE: ${btc:,.2f} ({source})", flush=True)
            
            # Window
            slug, start, end, secs = get_window()
            print(f"\nWINDOW: {slug}", flush=True)
            print(f"  Time left: {secs} seconds", flush=True)
            
            # Market data
            markets = get_market(slug)
            if markets:
                m = markets[0]
                tokens = m.get("clobTokenIds", [])
                outcomes = m.get("outcomes", [])
                if isinstance(tokens, str):
                    tokens = json.loads(tokens)
                if isinstance(outcomes, str):
                    outcomes = json.loads(outcomes)
                
                print(f"\nORDERBOOK:", flush=True)
                print(f"  {'Side':<6} {'Best Bid':<12} {'Best Ask':<12} {'Spread':<10} {'Ask Size':<10}", flush=True)
                print(f"  {'-'*50}", flush=True)
                
                for o, t in zip(outcomes, tokens):
                    book = get_book(t)
                    if book:
                        bids = book.get("bids", [])
                        asks = book.get("asks", [])
                        best_bid = float(bids[0]["price"]) if bids else 0
                        best_ask = float(asks[0]["price"]) if asks else 1
                        ask_size = float(asks[0]["size"]) if asks else 0
                        spread = (best_ask - best_bid) * 100
                        print(f"  {o:<6} {best_bid:<12.4f} {best_ask:<12.4f} {spread:<10.1f}c {ask_size:<10.0f}", flush=True)
                
                # Show deeper book levels
                print(f"\n  DEEPER ASKS (UP):", flush=True)
                for o, t in zip(outcomes, tokens):
                    if str(o).lower() == "up":
                        book = get_book(t)
                        if book:
                            asks = book.get("asks", [])[:10]
                            for i, a in enumerate(asks):
                                price = float(a["price"])
                                size = float(a["size"])
                                print(f"    Level {i+1}: {price:.4f} x {size:.0f} shares", flush=True)
            else:
                print("\n  No market data", flush=True)
            
            print(f"\n--- Refresh in 3 sec ---", flush=True)
            time.sleep(3)
            
        except KeyboardInterrupt:
            print("\nStopped", flush=True)
            break
        except Exception as e:
            print(f"Error: {e}", flush=True)
            time.sleep(3)

if __name__ == "__main__":
    main()

