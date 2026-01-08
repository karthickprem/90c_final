"""
Test CLOB client with proper initialization based on py-clob-client examples
Reference: https://github.com/polymarket/py-clob-client
"""

import json
from py_clob_client.client import ClobClient
from py_clob_client.clob_types import ApiCreds, OrderArgs, OrderType, BalanceAllowanceParams
from py_clob_client.order_builder.constants import BUY, SELL

# For proxy wallet users (Magic.link / email signup)
from py_clob_client.constants import POLYGON

CLOB_HOST = "https://clob.polymarket.com"

with open("pm_api_config.json") as f:
    config = json.load(f)

print("=" * 60)
print("POLYMARKET CLOB TEST - Proper Initialization")
print("=" * 60)

# Get the private key
private_key = config["private_key"]

# Create API credentials
creds = ApiCreds(
    api_key=config["api_key"],
    api_secret=config["api_secret"],
    api_passphrase=config["api_passphrase"],
)

# Test different client configurations
print("\n[Test 1] Standard EOA wallet...")
try:
    client = ClobClient(
        host=CLOB_HOST,
        key=private_key,
        chain_id=POLYGON,
        creds=creds,
    )
    
    address = client.get_address()
    print(f"  Address: {address}")
    
    # Try to get balance with proper params
    try:
        params = BalanceAllowanceParams(asset_type="USDC")
        result = client.get_balance_allowance(params)
        print(f"  Balance result: {result}")
    except Exception as e:
        print(f"  Balance error: {e}")
    
    # Try placing order
    token = "54278197475303842386505386968240270999114208289715750865719192918672390408055"
    order_args = OrderArgs(
        token_id=token,
        price=0.01,
        size=1.0,
        side=BUY,
    )
    
    signed = client.create_order(order_args)
    result = client.post_order(signed, OrderType.GTC)
    print(f"  Order result: {result}")
    
except Exception as e:
    print(f"  Error: {e}")

print("\n[Test 2] With funder parameter (for proxy wallets)...")
try:
    # For users who signed up via email, they use proxy wallets
    # The funder is the address that funds the trades
    
    client = ClobClient(
        host=CLOB_HOST,
        key=private_key,
        chain_id=POLYGON,
        creds=creds,
        funder=POLYGON,  # Use Polygon as funder
    )
    
    address = client.get_address()
    print(f"  Address: {address}")
    
    # Try placing order
    token = "54278197475303842386505386968240270999114208289715750865719192918672390408055"
    order_args = OrderArgs(
        token_id=token,
        price=0.01,
        size=1.0,
        side=BUY,
    )
    
    signed = client.create_order(order_args)
    result = client.post_order(signed, OrderType.GTC)
    print(f"  Order result: {result}")
    
except Exception as e:
    print(f"  Error: {e}")

print("\n[Test 3] Check if this is a proxy wallet...")
try:
    # The address from your screenshot might be different from the derived address
    # Let's check what address is associated with your API key
    
    import requests
    
    # Try to get the address associated with the API key
    headers = {
        "POLY_API_KEY": config["api_key"],
    }
    
    r = requests.get(f"{CLOB_HOST}/auth/api-key", headers=headers, timeout=10)
    print(f"  API key info: {r.status_code} - {r.text[:200] if r.text else 'empty'}")
    
except Exception as e:
    print(f"  Error: {e}")

print("\n" + "=" * 60)
print("If orders fail with 'not enough balance', you may need to:")
print("1. Use the correct wallet type (proxy vs EOA)")
print("2. Ensure the private key matches your Polymarket account")
print("=" * 60)

