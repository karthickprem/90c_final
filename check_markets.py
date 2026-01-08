"""
Check actual market status for each position token
"""

import json
import requests

with open("pm_api_config.json") as f:
    config = json.load(f)

proxy = config["proxy_address"]

print("=" * 70)
print("CHECKING MARKET STATUS FOR EACH POSITION")
print("=" * 70)

# Get positions
r = requests.get(f"https://data-api.polymarket.com/positions?user={proxy}", timeout=10)
positions = r.json()

for i, pos in enumerate(positions):
    outcome = pos.get("outcome", "?")
    token_id = pos.get("asset", "")
    size = float(pos.get("size", 0))
    avg = float(pos.get("avgPrice", 0))
    
    print(f"\n[{i+1}] {outcome}: {size:.2f} shares @ {avg*100:.0f}c (${size*avg:.2f})")
    print(f"    Token: {token_id[:50]}...")
    
    # Try to get market info from CLOB
    try:
        r = requests.get(f"https://clob.polymarket.com/book?token_id={token_id}", timeout=5)
        if r.status_code == 200:
            book = r.json()
            if book.get("bids") or book.get("asks"):
                print(f"    -> ORDERBOOK EXISTS - Market is OPEN")
                best_bid = book.get("bids", [{}])[0].get("price", 0) if book.get("bids") else 0
                best_ask = book.get("asks", [{}])[0].get("price", 0) if book.get("asks") else 0
                print(f"    -> Bid: {float(best_bid)*100:.0f}c | Ask: {float(best_ask)*100:.0f}c")
            else:
                print(f"    -> ORDERBOOK EMPTY - Market may be closed/resolved")
        elif r.status_code == 400:
            print(f"    -> ORDERBOOK DOES NOT EXIST - Market CLOSED")
        else:
            print(f"    -> Status: {r.status_code}")
    except Exception as e:
        print(f"    -> Error: {e}")
    
    # Try gamma API for market info
    try:
        r = requests.get(f"https://gamma-api.polymarket.com/markets?clob_token_ids={token_id}", timeout=5)
        if r.status_code == 200:
            markets = r.json()
            if markets:
                m = markets[0]
                print(f"    -> Market: {m.get('question', '?')[:50]}...")
                print(f"    -> Closed: {m.get('closed')} | Resolved: {m.get('resolved')}")
                if m.get('resolved'):
                    print(f"    -> Winner: {m.get('winning_outcome', 'Unknown')}")
    except:
        pass

print("\n" + "=" * 70)
print("NEXT STEPS:")
print("=" * 70)
print("""
1. For CLOSED markets with no orderbook:
   -> Go to Polymarket.com > Portfolio > Positions
   -> Look for 'Redeem' button on each position
   
2. For BTC 15-min Up/Down markets:
   -> These auto-settle after the window closes
   -> Check 'Activity' for settlement transactions
   
3. If positions show but no redemption available:
   -> The positions may already be settled
   -> The API data is cached/stale
   -> Your actual USDC is what matters: $3.93
""")

