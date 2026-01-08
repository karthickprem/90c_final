import time
import requests
import json

# Get current window
ts = int(time.time())
start = ts - (ts % 900)
end = start + 900
secs_left = end - ts

print(f"Current time: {ts}")
print(f"Window start: {start}")
print(f"Window end: {end}")
print(f"Seconds left: {secs_left} ({secs_left//60}:{secs_left%60:02d})")
print(f"Slug: btc-updown-15m-{start}")
print()

# Fetch market from Gamma API
slug = f"btc-updown-15m-{start}"
r = requests.get("https://gamma-api.polymarket.com/markets", params={"slug": slug})
if r.status_code == 200:
    markets = r.json()
    if markets:
        m = markets[0]
        print(f"Market: {m.get('question', '?')}")
        print(f"Closed: {m.get('closed', False)}")
        print(f"Accepting orders: {m.get('acceptingOrders', False)}")
        print(f"Outcomes: {m.get('outcomes')}")
        print(f"Outcome prices: {m.get('outcomePrices')}")
        
        # Get token IDs
        toks = m.get("clobTokenIds", [])
        outs = m.get("outcomes", [])
        if isinstance(toks, str):
            toks = json.loads(toks)
        if isinstance(outs, str):
            outs = json.loads(outs)
        
        print()
        print("Token IDs:")
        for o, t in zip(outs, toks):
            print(f"  {o}: {t[:30]}...")
        
        # Get live prices from CLOB
        print()
        print("Live prices from CLOB:")
        for o, t in zip(outs, toks):
            try:
                r2 = requests.get("https://clob.polymarket.com/midpoint", params={"token_id": t})
                if r2.status_code == 200:
                    mid = float(r2.json().get("mid", 0))
                    print(f"  {o}: {mid*100:.1f}c")
                else:
                    print(f"  {o}: Error {r2.status_code}")
            except Exception as e:
                print(f"  {o}: {e}")
    else:
        print("No market found for this slug!")
        print("Trying to find active BTC windows...")
        r3 = requests.get("https://gamma-api.polymarket.com/markets", 
                         params={"tag": "btc-up-or-down-15m", "active": True, "limit": 5})
        if r3.status_code == 200:
            for m in r3.json()[:5]:
                print(f"  - {m.get('slug')}: {m.get('question')}")
else:
    print(f"API error: {r.status_code}")

