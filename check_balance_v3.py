"""
Check Polymarket balance - Direct API
"""

import json
import time
import requests
import hmac
import hashlib
import base64

CLOB_HOST = "https://clob.polymarket.com"

with open("pm_api_config.json") as f:
    config = json.load(f)

print("=" * 60)
print("POLYMARKET BALANCE CHECK V3")
print("=" * 60)

api_key = config["api_key"]
api_secret = config["api_secret"]
api_passphrase = config["api_passphrase"]
address = "0xc88E524996e151089c740f164270C13fE1056C17"

print(f"\nWallet: {address}")
print(f"API Key: {api_key[:20]}...")

# Fix base64 padding
def fix_base64_padding(s):
    return s + '=' * (-len(s) % 4)

# Create signature
def create_signature(secret, timestamp, method, path, body=""):
    message = timestamp + method + path + body
    secret_decoded = base64.b64decode(fix_base64_padding(secret))
    signature = hmac.new(
        secret_decoded,
        message.encode('utf-8'),
        hashlib.sha256
    ).digest()
    return base64.b64encode(signature).decode()

# Get balance
print("\n[1] Getting balance...")
try:
    timestamp = str(int(time.time() * 1000))
    signature = create_signature(api_secret, timestamp, "GET", "/balance-allowance")
    
    headers = {
        "POLY_ADDRESS": address,
        "POLY_API_KEY": api_key,
        "POLY_PASSPHRASE": api_passphrase,
        "POLY_TIMESTAMP": timestamp,
        "POLY_SIGNATURE": signature,
    }
    
    r = requests.get(f"{CLOB_HOST}/balance-allowance", headers=headers, timeout=10)
    print(f"  Status: {r.status_code}")
    print(f"  Response: {r.text}")
    
    if r.status_code == 200:
        data = r.json()
        balance = float(data.get("balance", 0))
        allowance = float(data.get("allowance", 0))
        
        # USDC has 6 decimals
        if balance > 1e10:
            balance = balance / 1e6
        if allowance > 1e10:
            allowance = allowance / 1e6
        
        print(f"\n  Balance: ${balance:.6f}")
        print(f"  Allowance: ${allowance:.6f}")

except Exception as e:
    print(f"  Error: {e}")
    import traceback
    traceback.print_exc()

# Try without signature (some endpoints don't need it)
print("\n[2] Trying public endpoints...")
try:
    # Markets
    r = requests.get(f"{CLOB_HOST}/markets", timeout=10)
    print(f"  Markets endpoint: {r.status_code}")
    
    # Tick sizes
    r = requests.get(f"{CLOB_HOST}/tick-size", params={"token_id": "54278197475303842386505386968240270999114208289715750865719192918672390408055"}, timeout=10)
    print(f"  Tick size: {r.status_code} - {r.text[:100]}...")
    
except Exception as e:
    print(f"  Error: {e}")

print("\n" + "=" * 60)

