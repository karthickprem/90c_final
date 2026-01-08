"""
Test order with PROXY wallet configuration
Reference: https://github.com/Polymarket/magic-safe-builder-example
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
print("PROXY WALLET ORDER TEST")
print("=" * 60)

# Get current window
ts = int(time.time())
start = ts - (ts % 900)
slug = f"btc-updown-15m-{start}"
print(f"\nCurrent window: {slug}")

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
        print(f"UP token: {t[:40]}...")

# Create client with different signature types
creds = ApiCreds(
    api_key=config["api_key"],
    api_secret=config["api_secret"],
    api_passphrase=config["api_passphrase"],
)

# According to py-clob-client docs:
# signature_type=0: EOA (Externally Owned Account)
# signature_type=1: Poly Proxy
# signature_type=2: Poly Gnosis Safe

for sig_type in [1, 2]:  # Try proxy types
    print(f"\n[Signature Type {sig_type}]")
    try:
        client = ClobClient(
            host=CLOB_HOST,
            key=config["private_key"],
            chain_id=POLYGON,
            creds=creds,
            signature_type=sig_type,
        )
        
        print(f"  Address: {client.get_address()}")
        
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
            print(f"  SUCCESS with signature_type={sig_type}!")
            client.cancel(result["orderID"])
            break
            
    except Exception as e:
        print(f"  Error: {e}")

# Try with funder parameter
print("\n[With funder parameter]")
try:
    # The funder should be the proxy wallet address
    # Let's try to find it by looking at past trades
    
    # First, let's see what address holds the balance
    # The signer address is: 0xc88E524996e151089c740f164270C13fE1056C17
    # But the funds are in the proxy
    
    # Try to use the signer as funder
    signer = "0xc88E524996e151089c740f164270C13fE1056C17"
    
    client = ClobClient(
        host=CLOB_HOST,
        key=config["private_key"],
        chain_id=POLYGON,
        creds=creds,
        signature_type=1,  # Poly Proxy
        funder=signer,
    )
    
    order_args = OrderArgs(
        token_id=token_up,
        price=0.01,
        size=1.0,
        side=BUY,
    )
    
    signed = client.create_order(order_args)
    result = client.post_order(signed, OrderType.GTC)
    
    print(f"  Result: {result}")
    
except Exception as e:
    print(f"  Error: {e}")

print("\n" + "=" * 60)
print("NOTE: If all fail with 'not enough balance', the issue is:")
print("  - Your $19.12 is in a PROXY wallet (Safe)")
print("  - We need to find the PROXY address from Polymarket")
print("")
print("Please go to Polymarket app and check your wallet address:")
print("  1. Click on your profile/wallet icon")
print("  2. Look for 'Wallet Address' or similar")
print("  3. Share that address here")
print("=" * 60)

