"""
POLYMARKET LIVE DATA - Correct endpoints
"""

import requests
import json
import time

GAMMA_API = "https://gamma-api.polymarket.com"
CLOB_API = "https://clob.polymarket.com"

session = requests.Session()

def get_window():
    """Get current 15-min window."""
    ts = int(time.time())
    start = ts - (ts % 900)
    end = start + 900
    slug = f"btc-updown-15m-{start}"
    secs_left = end - ts
    mins = int(secs_left // 60)
    secs = int(secs_left % 60)
    return slug, secs_left, f"{mins}:{secs:02d}"

def get_market_data(slug):
    """Get market data from Gamma API."""
    try:
        r = session.get(f"{GAMMA_API}/markets", params={"slug": slug}, timeout=10)
        markets = r.json()
        if markets:
            m = markets[0]
            prices = m.get("outcomePrices", [])
            outcomes = m.get("outcomes", [])
            tokens = m.get("clobTokenIds", [])
            
            if isinstance(prices, str):
                prices = json.loads(prices)
            if isinstance(outcomes, str):
                outcomes = json.loads(outcomes)
            if isinstance(tokens, str):
                tokens = json.loads(tokens)
            
            result = {}
            for o, p, t in zip(outcomes, prices, tokens):
                result[o.lower()] = {
                    "price": float(p),
                    "token": t
                }
            
            return result, m.get("bestBid"), m.get("bestAsk")
    except:
        pass
    return None, None, None

def get_clob_prices(token_id):
    """Get buy/sell prices from CLOB API."""
    try:
        r_buy = session.get(f"{CLOB_API}/price", params={"token_id": token_id, "side": "BUY"}, timeout=5)
        r_sell = session.get(f"{CLOB_API}/price", params={"token_id": token_id, "side": "SELL"}, timeout=5)
        r_mid = session.get(f"{CLOB_API}/midpoint", params={"token_id": token_id}, timeout=5)
        
        buy = float(r_buy.json().get("price", 0))
        sell = float(r_sell.json().get("price", 0))
        mid = float(r_mid.json().get("mid", 0))
        
        return buy, sell, mid
    except:
        return 0, 0, 0

def main():
    print("=" * 70, flush=True)
    print("POLYMARKET LIVE - BTC 15min Up/Down", flush=True)
    print("=" * 70, flush=True)
    print(flush=True)
    
    last_slug = None
    
    for i in range(30):  # 30 prints
        slug, secs_left, time_str = get_window()
        
        # Get market data
        data, best_bid, best_ask = get_market_data(slug)
        
        if slug != last_slug:
            last_slug = slug
            print(f"\n*** WINDOW: {slug} ***\n", flush=True)
        
        if data and "up" in data and "down" in data:
            # Get CLOB prices for more detail
            up_buy, up_sell, up_mid = get_clob_prices(data["up"]["token"])
            down_buy, down_sell, down_mid = get_clob_prices(data["down"]["token"])
            
            # Gamma prices (what website shows)
            up_price = data["up"]["price"]
            down_price = data["down"]["price"]
            
            print(f"Time: {time_str:>6} | Up: {up_price*100:.1f}c (buy:{up_buy:.2f} sell:{up_sell:.2f}) | Down: {down_price*100:.1f}c (buy:{down_buy:.2f} sell:{down_sell:.2f})", flush=True)
        else:
            print(f"Time: {time_str:>6} | No data", flush=True)
        
        time.sleep(2)
    
    print("\nDone.", flush=True)

if __name__ == "__main__":
    main()
