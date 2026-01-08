"""
POLYMARKET LIVE DATA - 5 minute continuous print
"""

import requests
import json
import time

GAMMA_API = "https://gamma-api.polymarket.com"
CLOB_API = "https://clob.polymarket.com"

session = requests.Session()

def get_window():
    ts = int(time.time())
    start = ts - (ts % 900)
    end = start + 900
    slug = f"btc-updown-15m-{start}"
    secs_left = end - ts
    mins = int(secs_left // 60)
    secs = int(secs_left % 60)
    return slug, secs_left, f"{mins}:{secs:02d}"

def get_market_data(slug):
    try:
        r = session.get(f"{GAMMA_API}/markets", params={"slug": slug}, timeout=10)
        markets = r.json()
        if markets:
            m = markets[0]
            prices = m.get("outcomePrices", [])
            outcomes = m.get("outcomes", [])
            
            if isinstance(prices, str):
                prices = json.loads(prices)
            if isinstance(outcomes, str):
                outcomes = json.loads(outcomes)
            
            result = {}
            for o, p in zip(outcomes, prices):
                result[o.lower()] = float(p)
            
            return result
    except:
        pass
    return None

def main():
    print("=" * 60, flush=True)
    print("POLYMARKET LIVE - 5 MINUTE TEST", flush=True)
    print("=" * 60, flush=True)
    print("Printing every 3 seconds for 5 minutes (100 prints)", flush=True)
    print("=" * 60, flush=True)
    print(flush=True)
    
    start_time = time.time()
    duration = 5 * 60  # 5 minutes
    count = 0
    
    while time.time() - start_time < duration:
        count += 1
        slug, secs_left, time_str = get_window()
        data = get_market_data(slug)
        
        if data and "up" in data and "down" in data:
            up_c = int(data["up"] * 100)
            down_c = int(data["down"] * 100)
            
            timestamp = time.strftime("%H:%M:%S")
            print(f"[{count:3d}] {timestamp} | Timer: {time_str} | Up: {up_c}c | Down: {down_c}c", flush=True)
        else:
            print(f"[{count:3d}] No data", flush=True)
        
        time.sleep(3)
    
    print("\n" + "=" * 60, flush=True)
    print("5 MINUTE TEST COMPLETE", flush=True)
    print(f"Total prints: {count}", flush=True)
    print("=" * 60, flush=True)

if __name__ == "__main__":
    main()

