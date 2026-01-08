"""
TEST REAL ORDER - With correct configuration!
"""

import json
import time
import requests
from py_clob_client.client import ClobClient
from py_clob_client.clob_types import ApiCreds, OrderArgs, OrderType
from py_clob_client.order_builder.constants import BUY
from py_clob_client.constants import POLYGON

CLOB_HOST = "https://clob.polymarket.com"
GAMMA_API = "https://gamma-api.polymarket.com"

with open("pm_api_config.json") as f:
    config = json.load(f)

PROXY_ADDRESS = "0x3C008F983c1d1097a1304e38B683B018aC589500"

print("=" * 60)
print("REAL ORDER TEST")
print("=" * 60)

# Get current window
ts = int(time.time())
start = ts - (ts % 900)
slug = f"btc-updown-15m-{start}"
print(f"Window: {slug}")
print(f"Time left: {(start + 900 - ts) // 60}:{(start + 900 - ts) % 60:02d}")

# Get tokens
r = requests.get(f"{GAMMA_API}/markets", params={"slug": slug}, timeout=10)
markets = r.json()
m = markets[0]
tokens = m.get("clobTokenIds", [])
outcomes = m.get("outcomes", [])
if isinstance(tokens, str):
    tokens = json.loads(tokens)
if isinstance(outcomes, str):
    outcomes = json.loads(outcomes)

token_up = None
for o, t in zip(outcomes, tokens):
    if o.lower() == "up":
        token_up = t

# Create client with CORRECT configuration
creds = ApiCreds(
    api_key=config["api_key"],
    api_secret=config["api_secret"],
    api_passphrase=config["api_passphrase"],
)

client = ClobClient(
    host=CLOB_HOST,
    key=config["private_key"],
    chain_id=POLYGON,
    creds=creds,
    signature_type=1,  # Poly Proxy - THIS WORKS!
    funder=PROXY_ADDRESS,
)

print(f"\nClient ready!")
print(f"  Signer: {client.get_address()}")
print(f"  Funder: {PROXY_ADDRESS}")

# Place a test order at 1 cent (won't fill, just test)
print("\n[Placing test order at 1 cent - won't fill]")
try:
    order_args = OrderArgs(
        token_id=token_up,
        price=0.01,  # 1 cent - way below market
        size=10.0,   # 10 shares (above min of 5)
        side=BUY,
    )
    
    print(f"  Order: BUY 10 shares @ 1 cent = $0.10")
    
    signed = client.create_order(order_args)
    result = client.post_order(signed, OrderType.GTC)
    
    print(f"  Result: {result}")
    
    if result and result.get("orderID"):
        order_id = result["orderID"]
        print(f"\n  *** SUCCESS! Order placed! ***")
        print(f"  Order ID: {order_id}")
        
        # Cancel it immediately
        print("\n  Cancelling test order...")
        client.cancel(order_id)
        print("  Cancelled!")
        
        print("\n" + "=" * 60)
        print("ORDER PLACEMENT WORKS!")
        print("Ready to start live trading!")
        print("=" * 60)
    else:
        print(f"  Unexpected result: {result}")
        
except Exception as e:
    print(f"  Error: {e}")

print("\n" + "=" * 60)

