"""
Test with the user's PROXY wallet address
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

# PROXY wallet address (where the funds are)
PROXY_ADDRESS = "0x3C008F983c1d1097a1304e38B683B018aC589500"

print("=" * 60)
print("TEST WITH PROXY WALLET")
print("=" * 60)
print(f"\nSigner: {config['private_key'][:20]}...")
print(f"Proxy:  {PROXY_ADDRESS}")

# Get current window
ts = int(time.time())
start = ts - (ts % 900)
slug = f"btc-updown-15m-{start}"
print(f"\nWindow: {slug}")

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
        print(f"UP token: {t[:30]}...")

# Create credentials
creds = ApiCreds(
    api_key=config["api_key"],
    api_secret=config["api_secret"],
    api_passphrase=config["api_passphrase"],
)

# Test with proxy as funder
print("\n[Test 1] Signature Type 1 (Poly Proxy) with funder...")
try:
    client = ClobClient(
        host=CLOB_HOST,
        key=config["private_key"],
        chain_id=POLYGON,
        creds=creds,
        signature_type=1,  # Poly Proxy
        funder=PROXY_ADDRESS,
    )
    
    print(f"  Client address: {client.get_address()}")
    
    order_args = OrderArgs(
        token_id=token_up,
        price=0.01,
        size=1.0,
        side=BUY,
    )
    
    signed = client.create_order(order_args)
    result = client.post_order(signed, OrderType.GTC)
    
    print(f"  Result: {result}")
    
    if result and result.get("orderID"):
        print("  SUCCESS!")
        client.cancel(result["orderID"])
        
except Exception as e:
    print(f"  Error: {e}")

print("\n[Test 2] Signature Type 2 (Gnosis Safe) with funder...")
try:
    client = ClobClient(
        host=CLOB_HOST,
        key=config["private_key"],
        chain_id=POLYGON,
        creds=creds,
        signature_type=2,  # Gnosis Safe
        funder=PROXY_ADDRESS,
    )
    
    print(f"  Client address: {client.get_address()}")
    
    order_args = OrderArgs(
        token_id=token_up,
        price=0.01,
        size=1.0,
        side=BUY,
    )
    
    signed = client.create_order(order_args)
    result = client.post_order(signed, OrderType.GTC)
    
    print(f"  Result: {result}")
    
    if result and result.get("orderID"):
        print("  SUCCESS!")
        client.cancel(result["orderID"])
        
except Exception as e:
    print(f"  Error: {e}")

print("\n[Test 3] Signature Type 0 with funder...")
try:
    client = ClobClient(
        host=CLOB_HOST,
        key=config["private_key"],
        chain_id=POLYGON,
        creds=creds,
        signature_type=0,  # EOA
        funder=PROXY_ADDRESS,
    )
    
    print(f"  Client address: {client.get_address()}")
    
    order_args = OrderArgs(
        token_id=token_up,
        price=0.01,
        size=1.0,
        side=BUY,
    )
    
    signed = client.create_order(order_args)
    result = client.post_order(signed, OrderType.GTC)
    
    print(f"  Result: {result}")
    
    if result and result.get("orderID"):
        print("  SUCCESS!")
        client.cancel(result["orderID"])
        
except Exception as e:
    print(f"  Error: {e}")

print("\n" + "=" * 60)

