"""
Check Polymarket trading balance properly
"""

import json
import requests
from web3 import Web3

with open("pm_api_config.json") as f:
    config = json.load(f)

proxy = config["proxy_address"]
print(f"Proxy: {proxy}")

# Polygon RPC
w3 = Web3(Web3.HTTPProvider("https://polygon-rpc.com"))

# USDC contract on Polygon (6 decimals)
USDC = "0x2791Bca1f2de4661ED88A30C99A7a9449Aa84174"

# Check USDC balance in proxy wallet
usdc_abi = [{"constant":True,"inputs":[{"name":"account","type":"address"}],"name":"balanceOf","outputs":[{"name":"","type":"uint256"}],"type":"function"}]
usdc_contract = w3.eth.contract(address=Web3.to_checksum_address(USDC), abi=usdc_abi)

balance = usdc_contract.functions.balanceOf(Web3.to_checksum_address(proxy)).call()
usdc_balance = balance / 1e6

print(f"\nUSDC in proxy wallet: ${usdc_balance:.2f}")

# Check Polymarket Exchange contract balance
# Polymarket uses CTF Exchange at this address
EXCHANGE = "0x4bFb41d5B3570DeFd03C39a9A4D8dE6Bd8B8982E"

# The exchange might hold USDC on behalf of users
# But the actual mechanism is different - let's check collateral

# Polymarket uses Conditional Tokens Framework (CTF)
# Users deposit USDC which becomes collateral
# Let's check the Neg Risk CTF Adapter and Exchange

print("\n--- Checking Polymarket contracts ---")

# The exchange contract might expose user balances
# But this requires proper ABI

# Alternative: Check data-api for more info
print("\nChecking data-api endpoints...")

endpoints = [
    f"https://data-api.polymarket.com/balance?user={proxy}",
    f"https://data-api.polymarket.com/users/{proxy}/balance",
    f"https://clob-data.polymarket.com/balance/{proxy}",
]

for url in endpoints:
    try:
        r = requests.get(url, timeout=5)
        print(f"  {url.split('/')[-2]}/{url.split('/')[-1][:20]}: {r.status_code}")
        if r.status_code == 200 and r.text:
            print(f"    {r.text[:200]}")
    except Exception as e:
        print(f"  Error: {e}")

# The most reliable: check our last successful order
print("\n--- Summary ---")
print(f"USDC in wallet: ${usdc_balance:.2f}")
print(f"This is your tradeable balance for placing orders.")
print(f"\nIf Polymarket mobile shows more, it includes:")
print(f"  - Value of open positions")
print(f"  - Pending settlements")

