"""
Show all open positions and balance from Polymarket
"""

import json
import requests
from web3 import Web3

with open("pm_api_config.json") as f:
    config = json.load(f)

proxy = config["proxy_address"]
signer = "0xc88E524996e151089c740f164270C13fE1056C17"  # From private key

print("=" * 70)
print("POLYMARKET ACCOUNT STATUS")
print("=" * 70)
print(f"Proxy wallet:  {proxy}")
print(f"Signer wallet: {signer}")

# Check USDC balance in BOTH wallets
w3 = Web3(Web3.HTTPProvider("https://polygon-rpc.com"))
USDC = "0x2791Bca1f2de4661ED88A30C99A7a9449Aa84174"
usdc_abi = [{"constant":True,"inputs":[{"name":"account","type":"address"}],"name":"balanceOf","outputs":[{"name":"","type":"uint256"}],"type":"function"}]
usdc_contract = w3.eth.contract(address=Web3.to_checksum_address(USDC), abi=usdc_abi)

print("\n--- USDC BALANCES ---")
for name, addr in [("Proxy", proxy), ("Signer", signer)]:
    try:
        bal = usdc_contract.functions.balanceOf(Web3.to_checksum_address(addr)).call()
        print(f"{name}: ${bal/1e6:.2f}")
    except Exception as e:
        print(f"{name}: Error - {e}")

# Check positions from data-api
print("\n--- POSITIONS (data-api) ---")
for addr in [proxy, signer]:
    print(f"\nWallet: {addr[:20]}...")
    try:
        r = requests.get(f"https://data-api.polymarket.com/positions?user={addr}", timeout=10)
        if r.status_code == 200:
            positions = r.json()
            if positions:
                print(f"  Found {len(positions)} positions:")
                for p in positions:
                    size = float(p.get("size", 0))
                    avg = float(p.get("avgPrice", 0))
                    cur = float(p.get("currentPrice", 0))
                    outcome = p.get("outcome", "?")
                    market = p.get("market", {}).get("question", "?")[:50]
                    
                    cost = size * avg
                    value = size * cur
                    pnl = value - cost
                    
                    print(f"    {outcome}: {size:.2f} @ {avg*100:.0f}c -> {cur*100:.0f}c | Cost: ${cost:.2f} | Value: ${value:.2f} | P&L: ${pnl:+.2f}")
                    print(f"      Market: {market}...")
            else:
                print("  No positions")
        else:
            print(f"  Status: {r.status_code}")
    except Exception as e:
        print(f"  Error: {e}")

# Check portfolio value
print("\n--- PORTFOLIO VALUE ---")
for addr in [proxy, signer]:
    try:
        r = requests.get(f"https://data-api.polymarket.com/value?user={addr}", timeout=10)
        if r.status_code == 200:
            data = r.json()
            if data:
                value = data[0].get("value", 0)
                print(f"{addr[:20]}...: ${value:.2f}")
    except Exception as e:
        print(f"Error: {e}")

# Check activity/trades
print("\n--- RECENT ACTIVITY ---")
try:
    r = requests.get(f"https://data-api.polymarket.com/activity?user={proxy}&limit=10", timeout=10)
    if r.status_code == 200:
        activity = r.json()
        if activity:
            for a in activity[:5]:
                side = a.get("side", "?")
                size = a.get("size", 0)
                price = a.get("price", 0)
                outcome = a.get("outcome", "?")
                ts = a.get("timestamp", "?")
                print(f"  {side} {size} {outcome} @ {price} - {ts}")
        else:
            print("  No recent activity")
    else:
        print(f"  Status: {r.status_code}")
except Exception as e:
    print(f"  Error: {e}")

print("\n" + "=" * 70)

