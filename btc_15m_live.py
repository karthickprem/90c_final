"""
Live BTC 15m market scanner - find actual trading opportunities.
"""
import requests
import json
import time
from datetime import datetime

GAMMA_API = "https://gamma-api.polymarket.com"
CLOB_API = "https://clob.polymarket.com"

session = requests.Session()

def get_current_window():
    """Get the current 15-min window."""
    ts = int(time.time())
    ts = ts - (ts % 900)  # Round to 15 min
    return f"btc-updown-15m-{ts}"

def fetch_market(slug):
    """Fetch market by slug."""
    r = session.get(f"{GAMMA_API}/markets?slug={slug}", timeout=10)
    markets = r.json()
    return markets[0] if markets else None

def fetch_book(token_id):
    """Fetch orderbook."""
    r = session.get(f"{CLOB_API}/book", params={"token_id": token_id}, timeout=5)
    return r.json()

print("=" * 70)
print("BTC 15-MINUTE LIVE SCANNER")
print("=" * 70)

# Get current window
slug = get_current_window()
print(f"\nCurrent window: {slug}")

market = fetch_market(slug)
if not market:
    print("Market not found!")
    exit()

print(f"Question: {market.get('question')}")
print(f"End date: {market.get('endDate')}")
print(f"Liquidity: ${float(market.get('liquidity', 0)):,.0f}")

# Parse outcomes and tokens
outcomes = market.get("outcomes", [])
if isinstance(outcomes, str):
    outcomes = json.loads(outcomes)

tokens = market.get("clobTokenIds", [])
if isinstance(tokens, str):
    tokens = json.loads(tokens)

print(f"\nOutcomes: {outcomes}")
print(f"Tokens: {len(tokens)}")

# Fetch orderbooks
print("\n" + "-" * 70)
print("ORDERBOOKS:")
print("-" * 70)

ask_sum = 0
bids = {}
asks = {}

for i, (outcome, token) in enumerate(zip(outcomes, tokens)):
    book = fetch_book(token)
    
    bid_list = book.get("bids", [])
    ask_list = book.get("asks", [])
    
    if bid_list and ask_list:
        best_bid = float(bid_list[0]["price"])
        best_ask = float(ask_list[0]["price"])
        bid_size = float(bid_list[0]["size"])
        ask_size = float(ask_list[0]["size"])
        spread = (best_ask - best_bid) * 100
        
        bids[outcome] = best_bid
        asks[outcome] = best_ask
        ask_sum += best_ask
        
        print(f"\n{outcome}:")
        print(f"  Best bid: {best_bid:.4f} (${bid_size:.0f})")
        print(f"  Best ask: {best_ask:.4f} (${ask_size:.0f})")
        print(f"  Spread: {spread:.2f}c")
        
        # Show depth
        print(f"  Bid depth (top 5):")
        for level in bid_list[:5]:
            p = float(level["price"])
            s = float(level["size"])
            print(f"    {p:.4f} x ${s:.0f}")
        
        print(f"  Ask depth (top 5):")
        for level in ask_list[:5]:
            p = float(level["price"])
            s = float(level["size"])
            print(f"    {p:.4f} x ${s:.0f}")
    else:
        print(f"\n{outcome}: No orderbook")

# ARB CHECK
print("\n" + "=" * 70)
print("ARB ANALYSIS:")
print("=" * 70)

print(f"\nAsk sum: {ask_sum:.4f}")
if ask_sum < 1.0:
    edge = (1 - ask_sum) * 100
    print(f"*** ARB OPPORTUNITY: {edge:.2f}c edge! ***")
    print(f"Buy both sides at asks, guaranteed profit of {edge:.2f}c per $1")
else:
    gap = (ask_sum - 1) * 100
    print(f"No instant arb. Gap: +{gap:.2f}c above $1")

# Check for spread capture opportunity
print("\nSPREAD CAPTURE:")
for outcome in outcomes:
    if outcome in bids and outcome in asks:
        spread = (asks[outcome] - bids[outcome]) * 100
        if spread < 3:
            print(f"  {outcome}: TIGHT SPREAD {spread:.2f}c - MM opportunity!")
        else:
            print(f"  {outcome}: Spread {spread:.2f}c (too wide for MM)")

# Time remaining
end_ts = datetime.fromisoformat(market.get("endDate").replace("Z", "+00:00"))
now = datetime.now(end_ts.tzinfo)
remaining = (end_ts - now).total_seconds()
print(f"\nTime remaining: {remaining:.0f} seconds ({remaining/60:.1f} minutes)")

# Profit opportunity check
print("\n" + "=" * 70)
print("TRADING RECOMMENDATION:")
print("=" * 70)

if ask_sum < 1.0:
    print("ACTION: BUY BOTH SIDES NOW - Instant arb available!")
elif remaining < 120 and remaining > 0:
    print("LATE WINDOW: Watch for mispricing as settlement approaches")
    # Check if one side is cheap relative to probability
    for outcome in outcomes:
        if outcome in asks:
            ask = asks[outcome]
            if ask < 0.50 and ask > 0.05:  # Mid-range price
                print(f"  {outcome} trading at {ask:.4f} - potential value play")
else:
    print("NO IMMEDIATE OPPORTUNITY")
    print("  - Ask sum > 1 (no arb)")
    print("  - Spreads too wide for MM")
    print("  - Monitor for late-window mispricing")

