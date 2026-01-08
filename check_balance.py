"""
Check Polymarket balance and allowance status
"""

import json
from py_clob_client.client import ClobClient
from py_clob_client.clob_types import ApiCreds

CLOB_HOST = "https://clob.polymarket.com"
CHAIN_ID = 137

with open("pm_api_config.json") as f:
    config = json.load(f)

creds = ApiCreds(
    api_key=config["api_key"],
    api_secret=config["api_secret"],
    api_passphrase=config["api_passphrase"],
)

client = ClobClient(
    CLOB_HOST,
    key=config["private_key"],
    chain_id=CHAIN_ID,
    creds=creds
)

print("=" * 60)
print("POLYMARKET BALANCE CHECK")
print("=" * 60)

# Get wallet address
address = client.get_address()
print(f"\nWallet: {address}")

# Try to get balance/allowance
print("\n[1] Checking balance and allowance...")
try:
    result = client.get_balance_allowance()
    print(f"    Raw result: {result}")
    
    if result:
        balance = result.get("balance", 0)
        allowance = result.get("allowance", 0)
        
        # Convert if in wei
        if isinstance(balance, str):
            balance = float(balance)
        if isinstance(allowance, str):
            allowance = float(allowance)
        
        # USDC has 6 decimals
        if balance > 1e10:
            balance = balance / 1e6
        if allowance > 1e10:
            allowance = allowance / 1e6
        
        print(f"    Balance: ${balance:.2f}")
        print(f"    Allowance: ${allowance:.2f}")
        
        if allowance < balance:
            print("\n[!] Allowance is less than balance - need to approve!")
except Exception as e:
    print(f"    Error: {e}")

# Try to set/update allowance
print("\n[2] Attempting to set allowance...")
try:
    # This should approve the CLOB contract to spend USDC
    result = client.set_allowance()
    print(f"    Allowance set: {result}")
except Exception as e:
    print(f"    Error: {e}")

# Check again
print("\n[3] Re-checking balance...")
try:
    result = client.get_balance_allowance()
    print(f"    Result: {result}")
except Exception as e:
    print(f"    Error: {e}")

print("\n" + "=" * 60)
print("If balance shows $0, your USDC might be in your wallet")
print("but not deposited to Polymarket's trading contract.")
print("")
print("To deposit:")
print("1. Go to polymarket.com")
print("2. Click on your wallet/balance")
print("3. Deposit USDC to start trading")
print("=" * 60)

