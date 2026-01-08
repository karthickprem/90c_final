"""
Check Polymarket balance - trying different client init methods
"""

import json
import time
import requests
import hmac
import hashlib
import base64

from py_clob_client.client import ClobClient
from py_clob_client.clob_types import ApiCreds
from py_clob_client.constants import POLYGON

CLOB_HOST = "https://clob.polymarket.com"

with open("pm_api_config.json") as f:
    config = json.load(f)

print("=" * 60)
print("POLYMARKET BALANCE CHECK V2")
print("=" * 60)

# Method 1: Try with funder parameter
print("\n[Method 1] With funder=POLYGON...")
try:
    creds = ApiCreds(
        api_key=config["api_key"],
        api_secret=config["api_secret"],
        api_passphrase=config["api_passphrase"],
    )
    
    client = ClobClient(
        CLOB_HOST,
        key=config["private_key"],
        chain_id=POLYGON,
        creds=creds,
        signature_type=2,  # EIP712
        funder=POLYGON
    )
    
    address = client.get_address()
    print(f"  Wallet: {address}")
    
    result = client.get_balance_allowance()
    print(f"  Balance result: {result}")
    
except Exception as e:
    print(f"  Error: {e}")

# Method 2: Without funder
print("\n[Method 2] Without funder, signature_type=0...")
try:
    client = ClobClient(
        CLOB_HOST,
        key=config["private_key"],
        chain_id=137,
        creds=creds,
        signature_type=0
    )
    
    result = client.get_balance_allowance()
    print(f"  Balance result: {result}")
    
except Exception as e:
    print(f"  Error: {e}")

# Method 3: Direct API call
print("\n[Method 3] Direct API call...")
import requests
import hmac
import hashlib
import base64

try:
    address = "0xc88E524996e151089c740f164270C13fE1056C17"
    
    # Headers for authenticated request
    timestamp = str(int(time.time() * 1000))
    
    headers = {
        "POLY_ADDRESS": address,
        "POLY_API_KEY": config["api_key"],
        "POLY_PASSPHRASE": config["api_passphrase"],
        "POLY_TIMESTAMP": timestamp,
    }
    
    # Create signature
    message = timestamp + "GET" + "/balance-allowance"
    signature = hmac.new(
        base64.b64decode(config["api_secret"]),
        message.encode(),
        hashlib.sha256
    ).digest()
    headers["POLY_SIGNATURE"] = base64.b64encode(signature).decode()
    
    r = requests.get(f"{CLOB_HOST}/balance-allowance", headers=headers, timeout=10)
    print(f"  Status: {r.status_code}")
    print(f"  Response: {r.text}")
    
except Exception as e:
    print(f"  Error: {e}")

