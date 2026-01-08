"""
Simple test - just try to place a tiny order
The error message will tell us what's wrong
"""

import json
from py_clob_client.client import ClobClient
from py_clob_client.clob_types import ApiCreds, OrderArgs, OrderType
from py_clob_client.order_builder.constants import BUY

CLOB_HOST = "https://clob.polymarket.com"

with open("pm_api_config.json") as f:
    config = json.load(f)

creds = ApiCreds(
    api_key=config["api_key"],
    api_secret=config["api_secret"],
    api_passphrase=config["api_passphrase"],
)

print("=" * 60)
print("SIMPLE ORDER TEST")
print("=" * 60)

# Try creating client with different signature types
for sig_type in [0, 1, 2]:
    print(f"\n[Signature Type {sig_type}]")
    try:
        client = ClobClient(
            CLOB_HOST,
            key=config["private_key"],
            chain_id=137,
            creds=creds,
            signature_type=sig_type
        )
        
        address = client.get_address()
        print(f"  Wallet: {address}")
        
        # Try placing a tiny order
        token = "54278197475303842386505386968240270999114208289715750865719192918672390408055"
        
        order_args = OrderArgs(
            token_id=token,
            price=0.01,  # 1 cent - won't fill
            size=1.0,    # 1 share = 1 cent
            side=BUY,
        )
        
        print("  Creating order...")
        signed = client.create_order(order_args)
        
        print("  Posting order...")
        result = client.post_order(signed, OrderType.GTC)
        
        print(f"  Result: {result}")
        
        if result and result.get("orderID"):
            print("  SUCCESS! Order placed!")
            # Cancel it
            client.cancel(result["orderID"])
            print("  Order cancelled")
            break
        
    except Exception as e:
        print(f"  Error: {e}")

print("\n" + "=" * 60)

