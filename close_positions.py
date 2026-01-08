"""
Close/Settle ALL open positions
"""

import json
import requests
from py_clob_client.client import ClobClient
from py_clob_client.clob_types import ApiCreds, OrderArgs, OrderType
from py_clob_client.order_builder.constants import SELL
from py_clob_client.constants import POLYGON

with open("pm_api_config.json") as f:
    config = json.load(f)

proxy = config["proxy_address"]

print("=" * 70)
print("CLOSING ALL POSITIONS")
print("=" * 70)

# Get all positions
print("\n[1] Fetching positions...")
r = requests.get(f"https://data-api.polymarket.com/positions?user={proxy}", timeout=10)
positions = r.json()

if not positions:
    print("No positions found!")
    exit()

print(f"Found {len(positions)} positions:\n")

# Create client
creds = ApiCreds(
    api_key=config["api_key"],
    api_secret=config["api_secret"],
    api_passphrase=config["api_passphrase"],
)

client = ClobClient(
    host="https://clob.polymarket.com",
    key=config["private_key"],
    chain_id=POLYGON,
    creds=creds,
    signature_type=1,
    funder=proxy,
)

# Process each position
for i, pos in enumerate(positions):
    size = float(pos.get("size", 0))
    outcome = pos.get("outcome", "?")
    token_id = pos.get("asset", "")
    avg_price = float(pos.get("avgPrice", 0))
    current_price = float(pos.get("currentPrice", 0))
    
    # Get market info
    market = pos.get("market", {})
    question = market.get("question", "Unknown")[:50]
    closed = market.get("closed", False)
    resolved = market.get("resolved", False)
    
    cost = size * avg_price
    value = size * current_price if current_price > 0 else 0
    
    print(f"[{i+1}] {outcome}: {size:.2f} shares")
    print(f"    Market: {question}...")
    print(f"    Cost: ${cost:.2f} | Current value: ${value:.2f}")
    print(f"    Closed: {closed} | Resolved: {resolved}")
    
    if size <= 0:
        print("    -> No shares to sell")
        continue
    
    if resolved:
        print("    -> Market resolved - check for redemption on Polymarket")
        continue
    
    if closed:
        print("    -> Market closed - waiting for resolution")
        continue
    
    if not token_id:
        print("    -> No token ID, cannot sell")
        continue
    
    # Try to sell at current price or lower
    if current_price > 0:
        sell_price = max(0.01, current_price - 0.02)  # Sell 2c below current
    else:
        sell_price = 0.01  # Minimum price
    
    print(f"    -> Attempting to SELL {size:.2f} @ {sell_price*100:.0f}c...")
    
    try:
        order_args = OrderArgs(
            token_id=token_id,
            price=sell_price,
            size=size,
            side=SELL,
        )
        
        signed = client.create_order(order_args)
        result = client.post_order(signed, OrderType.GTC)
        
        if result and result.get("success"):
            print(f"    -> SELL order placed: {result.get('orderID', '')[:30]}...")
        else:
            print(f"    -> Order failed: {result}")
            
    except Exception as e:
        err = str(e)
        if "not enough" in err.lower():
            print(f"    -> Cannot sell - no shares available (may be settled)")
        else:
            print(f"    -> Error: {err[:80]}")
    
    print()

print("=" * 70)
print("DONE")
print("=" * 70)
print("\nNote: Some positions may auto-settle (BTC 15-min markets)")
print("Check Polymarket app for any 'Redeem' buttons")

