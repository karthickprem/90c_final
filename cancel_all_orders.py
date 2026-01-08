"""Cancel ALL open orders and show status"""

import json
from py_clob_client.client import ClobClient
from py_clob_client.clob_types import ApiCreds
from py_clob_client.constants import POLYGON

with open("pm_api_config.json") as f:
    config = json.load(f)

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
    funder=config["proxy_address"],
)

print("=" * 60)
print("CANCELLING ALL ORDERS")
print("=" * 60)

# Method 1: cancel_all
print("\n[1] Calling cancel_all()...")
try:
    result = client.cancel_all()
    print(f"    Result: {result}")
except Exception as e:
    print(f"    Error: {e}")

# Method 2: Get orders and cancel individually
print("\n[2] Getting open orders...")
try:
    orders = client.get_orders()
    if orders:
        print(f"    Found {len(orders)} orders")
        for o in orders:
            oid = o.get("id") or o.get("orderID") or o.get("order_id")
            status = o.get("status", "?")
            print(f"    - {oid[:30] if oid else '?'}... ({status})")
            if oid:
                try:
                    client.cancel(oid)
                    print(f"      Cancelled!")
                except Exception as e:
                    print(f"      Cancel error: {e}")
    else:
        print("    No open orders found")
except Exception as e:
    print(f"    Error: {e}")

print("\n" + "=" * 60)
print("DONE - All orders should be cancelled")
print("=" * 60)

