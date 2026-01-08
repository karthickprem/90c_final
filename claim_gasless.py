"""
Gasless claim using Polymarket Relayer
Polymarket sponsors the gas - no MATIC needed!
"""

import json
import requests
import time
from web3 import Web3

# Config
RELAYER_URL = "https://relayer-v2.polymarket.com"
CTF_ADDRESS = "0x4D97DCd97eC945f40cF65F87097ACe5EA0476045"
USDC_ADDRESS = "0x2791Bca1f2de4661ED88A30C99A7a9449Aa84174"

# Load credentials
with open("pm_api_config.json") as f:
    config = json.load(f)

proxy = config["proxy_address"]
api_key = config["api_key"]
api_secret = config["api_secret"]
api_passphrase = config["api_passphrase"]

print("=" * 60)
print("GASLESS CLAIM VIA POLYMARKET RELAYER")
print("=" * 60)
print(f"Proxy wallet: {proxy}")
print("Using: Polymarket Relayer (no MATIC needed!)")
print()

# Step 1: Get redeemable positions
print("Fetching redeemable positions...")
r = requests.get(f"https://data-api.polymarket.com/positions", params={"user": proxy}, timeout=10)

if r.status_code != 200:
    print(f"ERROR: Cannot fetch positions")
    exit(1)

positions = r.json()
redeemable = [p for p in positions if p.get("redeemable", False) and p.get("currentValue", 0) > 0]

print(f"Found {len(positions)} total positions")
print(f"Redeemable with value: {len(redeemable)}")

if not redeemable:
    print("\nNo positions to claim!")
    exit(0)

# Step 2: Build redemption calldata for each position
print(f"\n{'='*60}")
print("BUILDING REDEMPTION TRANSACTIONS...")
print(f"{'='*60}")

w3 = Web3()  # Just for encoding, no RPC needed

ctf_abi = [
    {"inputs": [{"name": "collateralToken", "type": "address"},
                {"name": "parentCollectionId", "type": "bytes32"},
                {"name": "conditionId", "type": "bytes32"},
                {"name": "indexSets", "type": "uint256[]"}],
     "name": "redeemPositions", "outputs": [],
     "stateMutability": "nonpayable", "type": "function"}
]

ctf = w3.eth.contract(address=Web3.to_checksum_address(CTF_ADDRESS), abi=ctf_abi)

transactions = []

for pos in redeemable:
    condition_id = pos.get("conditionId")
    value = pos.get("currentValue", 0)
    title = pos.get("title", "Unknown")
    
    print(f"\nPosition: {title[:50]}")
    print(f"  Value: ${value:.2f}")
    print(f"  Condition: {condition_id[:40]}...")
    
    # Build calldata
    condition_bytes = Web3.to_bytes(hexstr=condition_id)
    parent_collection = Web3.to_bytes(hexstr="0x" + "00" * 32)
    
    calldata = ctf.functions.redeemPositions(
        Web3.to_checksum_address(USDC_ADDRESS),
        parent_collection,
        condition_bytes,
        [1, 2]
    )._encode_transaction_data()
    
    transactions.append({
        "to": CTF_ADDRESS,
        "data": calldata,
        "value": "0",
        "title": title,
        "expected_value": value
    })
    
    print(f"  Calldata built: {calldata[:66]}...")

# Step 3: Submit to Polymarket Relayer
print(f"\n{'='*60}")
print("SUBMITTING TO RELAYER (GASLESS)...")
print(f"{'='*60}")

# Build relayer request
# Note: Using basic HTTP auth with API credentials
session = requests.Session()
session.auth = (api_key, api_secret)
session.headers.update({
    "Content-Type": "application/json",
    "x-passphrase": api_passphrase
})

for i, tx in enumerate(transactions):
    print(f"\nTransaction {i+1}/{len(transactions)}: {tx['title'][:40]}")
    print(f"  Expected: ${tx['expected_value']:.2f}")
    
    payload = {
        "transactions": [{
            "to": tx["to"],
            "data": tx["data"],
            "value": tx["value"]
        }],
        "type": "PROXY",  # Proxy wallet type
        "chainId": 137
    }
    
    try:
        r = session.post(f"{RELAYER_URL}/execute", json=payload, timeout=30)
        
        print(f"  Relayer response: {r.status_code}")
        
        if r.status_code == 200:
            result = r.json()
            print(f"  Result: {result}")
            print(f"  SUCCESS - Polymarket sponsored the gas!")
        else:
            print(f"  ERROR: {r.status_code} - {r.text[:200]}")
            
    except Exception as e:
        print(f"  ERROR: {str(e)[:100]}")

# Step 4: Check if balance increased
print(f"\n{'='*60}")
print("CHECKING BALANCE...")
print(f"{'='*60}")

time.sleep(5)  # Wait for settlement

w3_rpc = Web3(Web3.HTTPProvider("https://polygon-rpc.com"))
usdc_abi = [{"inputs":[{"name":"account","type":"address"}],
             "name":"balanceOf","outputs":[{"name":"","type":"uint256"}],"type":"function"}]
usdc = w3_rpc.eth.contract(address=Web3.to_checksum_address(USDC_ADDRESS), abi=usdc_abi)

balance_after = usdc.functions.balanceOf(Web3.to_checksum_address(proxy)).call() / 1e6

print(f"Cash balance AFTER: ${balance_after:.2f}")
print()

if balance_after > 2.5:  # Should be ~$8.24
    print("SUCCESS! Positions claimed!")
    print(f"Ready to trade with ${balance_after:.2f}")
else:
    print("Claims may not have processed yet")
    print("Try manual claim on Polymarket website")

