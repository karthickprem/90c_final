"""
Test with CURRENT window token ID
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

print("=" * 60)
print("LIVE ORDER TEST - Current Window")
print("=" * 60)

# Get current window
ts = int(time.time())
start = ts - (ts % 900)
slug = f"btc-updown-15m-{start}"
print(f"\nCurrent window: {slug}")
print(f"Time left: {(start + 900 - ts) // 60}:{(start + 900 - ts) % 60:02d}")

# Get tokens
r = requests.get(f"{GAMMA_API}/markets", params={"slug": slug}, timeout=10)
markets = r.json()

if not markets:
    print("No market found!")
    exit()

m = markets[0]
tokens = m.get("clobTokenIds", [])
outcomes = m.get("outcomes", [])

if isinstance(tokens, str):
    tokens = json.loads(tokens)
if isinstance(outcomes, str):
    outcomes = json.loads(outcomes)

token_up = None
for o, t in zip(outcomes, tokens):
    print(f"  {o}: {t[:40]}...")
    if o.lower() == "up":
        token_up = t

# Create client
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
)

print(f"\nWallet: {client.get_address()}")

# Try to place a tiny order
print("\n[Placing test order...]")
try:
    order_args = OrderArgs(
        token_id=token_up,
        price=0.01,  # 1 cent
        size=1.0,    # 1 share
        side=BUY,
    )
    
    print("  Creating signed order...")
    signed = client.create_order(order_args)
    
    print("  Posting order...")
    result = client.post_order(signed, OrderType.GTC)
    
    print(f"  Result: {result}")
    
    if result and result.get("orderID"):
        order_id = result["orderID"]
        print(f"  SUCCESS! Order ID: {order_id[:40]}...")
        
        # Cancel it
        print("  Cancelling...")
        client.cancel(order_id)
        print("  Cancelled!")
    
except Exception as e:
    print(f"  ERROR: {e}")

print("\n" + "=" * 60)

