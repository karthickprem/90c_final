"""
Test all possible ways to read balance from Polymarket
"""

import json
import requests
from py_clob_client.client import ClobClient
from py_clob_client.clob_types import ApiCreds, BalanceAllowanceParams
from py_clob_client.constants import POLYGON

with open("pm_api_config.json") as f:
    config = json.load(f)

proxy = config["proxy_address"]
print(f"Proxy wallet: {proxy}")
print("=" * 60)

# Method 1: data-api value endpoint
print("\n[1] data-api.polymarket.com/value")
try:
    r = requests.get(f"https://data-api.polymarket.com/value?user={proxy}", timeout=10)
    print(f"    Status: {r.status_code}")
    print(f"    Response: {r.json()}")
except Exception as e:
    print(f"    Error: {e}")

# Method 2: CLOB client balance
print("\n[2] CLOB client get_balance_allowance")
try:
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
        funder=proxy,
    )
    
    # Try different asset types
    for asset in ["USDC", "usdc", "0x2791Bca1f2de4661ED88A30C99A7a9449Aa84174"]:
        try:
            params = BalanceAllowanceParams(asset_type=asset)
            result = client.get_balance_allowance(params)
            print(f"    Asset '{asset}': {result}")
        except Exception as e:
            print(f"    Asset '{asset}': Error - {str(e)[:80]}")
except Exception as e:
    print(f"    Error: {e}")

# Method 3: Direct polygon RPC for USDC balance
print("\n[3] Direct USDC balance check (Polygon)")
try:
    # USDC contract on Polygon
    usdc_contract = "0x2791Bca1f2de4661ED88A30C99A7a9449Aa84174"
    
    # Use public RPC
    payload = {
        "jsonrpc": "2.0",
        "method": "eth_call",
        "params": [{
            "to": usdc_contract,
            "data": f"0x70a08231000000000000000000000000{proxy[2:].lower()}"  # balanceOf(address)
        }, "latest"],
        "id": 1
    }
    
    r = requests.post("https://polygon-rpc.com", json=payload, timeout=10)
    if r.status_code == 200:
        result = r.json().get("result", "0x0")
        balance = int(result, 16) / 1e6  # USDC has 6 decimals
        print(f"    USDC in wallet: ${balance:.2f}")
except Exception as e:
    print(f"    Error: {e}")

# Method 4: Check Polymarket exchange contract balance
print("\n[4] Polymarket Exchange USDC balance")
try:
    # Polymarket uses CTF Exchange contract
    # The balance might be in the exchange contract, not the wallet directly
    exchange = "0x4bFb41d5B3570DeFd03C39a9A4D8dE6Bd8B8982E"  # CTF Exchange
    
    payload = {
        "jsonrpc": "2.0",
        "method": "eth_call",
        "params": [{
            "to": usdc_contract,
            "data": f"0x70a08231000000000000000000000000{exchange[2:].lower()}"
        }, "latest"],
        "id": 1
    }
    
    # This won't work directly - need to check user's balance IN the exchange
    print("    (Requires specific contract call - checking data-api instead)")
except Exception as e:
    print(f"    Error: {e}")

# Method 5: Check profile/wallet endpoint
print("\n[5] Polymarket profile endpoints")
for endpoint in [
    f"https://gamma-api.polymarket.com/wallets/{proxy}",
    f"https://data-api.polymarket.com/wallet/{proxy}",
    f"https://strapi-matic.poly.market/wallets?address={proxy}",
]:
    try:
        r = requests.get(endpoint, timeout=5)
        print(f"    {endpoint.split('/')[-1][:30]}: {r.status_code} - {r.text[:100] if r.text else 'empty'}")
    except Exception as e:
        print(f"    Error: {e}")

print("\n" + "=" * 60)
print("The 'value' from data-api seems to be portfolio value.")
print("For trading balance, the CLOB uses internal accounting.")
print("=" * 60)

