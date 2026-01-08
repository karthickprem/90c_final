"""
Get proxy wallet address for Magic.link users
Reference: https://github.com/Polymarket/py-clob-client
"""

import json
import requests
from py_clob_client.client import ClobClient
from py_clob_client.clob_types import ApiCreds
from py_clob_client.constants import POLYGON

CLOB_HOST = "https://clob.polymarket.com"

with open("pm_api_config.json") as f:
    config = json.load(f)

print("=" * 60)
print("PROXY WALLET DETECTION")
print("=" * 60)

creds = ApiCreds(
    api_key=config["api_key"],
    api_secret=config["api_secret"],
    api_passphrase=config["api_passphrase"],
)

# Your signer address
client = ClobClient(
    host=CLOB_HOST,
    key=config["private_key"],
    chain_id=POLYGON,
    creds=creds,
)

signer_address = client.get_address()
print(f"\nSigner address: {signer_address}")

# Check if there's a proxy wallet associated
print("\n[1] Checking for proxy wallet via derive_api_key response...")
try:
    # When we derived the API key, the response should contain proxy info
    # Let's try to get it again
    result = client.derive_api_key()
    print(f"  API key info: {result}")
except Exception as e:
    print(f"  Error: {e}")

# Check the polyaddress header approach
print("\n[2] Checking Polymarket profile API...")
try:
    # This might return the proxy wallet
    r = requests.get(
        f"https://gamma-api.polymarket.com/profiles/{signer_address}",
        timeout=10
    )
    print(f"  Status: {r.status_code}")
    if r.status_code == 200:
        print(f"  Profile: {r.json()}")
except Exception as e:
    print(f"  Error: {e}")

# Try the /positions endpoint which might reveal the trading wallet
print("\n[3] Checking positions...")
try:
    # The positions endpoint might reveal the actual trading wallet
    headers = {
        "Authorization": f"Bearer {config['api_key']}",
    }
    r = requests.get(
        f"{CLOB_HOST}/data/position",
        headers=headers,
        timeout=10
    )
    print(f"  Status: {r.status_code}")
    print(f"  Response: {r.text[:500] if r.text else 'empty'}")
except Exception as e:
    print(f"  Error: {e}")

print("\n[4] Looking for Safe wallet...")
# The Safe wallet address can be derived from the signer
# For Polymarket, the proxy is usually a Safe (Gnosis Safe)
try:
    # Check if there's a Safe associated with this signer
    # Safe factory address on Polygon
    # This would require web3 library to properly compute
    print("  Need to check on Polygonscan for Safe wallet...")
    print(f"  Check: https://polygonscan.com/address/{signer_address}")
except Exception as e:
    print(f"  Error: {e}")

print("\n" + "=" * 60)
print("POSSIBLE SOLUTIONS:")
print("=" * 60)
print("""
1. Your balance ($19.12) is in a PROXY wallet, not the signer wallet
2. The private key from Magic.link is the SIGNER for that proxy

To trade via API, you need to:
A) Find your proxy wallet address (from Polymarket UI or Polygonscan)
B) Initialize the client with the proxy address as 'funder'

OR

C) Use the Polymarket website to trade manually
D) Wait for the paper trading results before going live
""")

