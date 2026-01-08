"""
Simple script to claim all redeemable positions
"""

import json
import requests
from web3 import Web3
from eth_account import Account

RPC_URL = "https://polygon-rpc.com"
USDC_ADDRESS = "0x2791Bca1f2de4661ED88A30C99A7a9449Aa84174"
CTF_ADDRESS = "0x4D97DCd97eC945f40cF65F87097ACe5EA0476045"

# Load config
with open("pm_api_config.json") as f:
    config = json.load(f)

proxy = config["proxy_address"]
private_key = config["private_key"]

print("=" * 60)
print("CLAIM UNREDEEMED WINNINGS")
print("=" * 60)
print(f"Proxy wallet: {proxy}")
print()

# Step 1: Check current cash balance
w3 = Web3(Web3.HTTPProvider(RPC_URL))
usdc_abi = [{"inputs":[{"name":"account","type":"address"}],
             "name":"balanceOf","outputs":[{"name":"","type":"uint256"}],"type":"function"}]
usdc = w3.eth.contract(address=Web3.to_checksum_address(USDC_ADDRESS), abi=usdc_abi)

balance_before = usdc.functions.balanceOf(Web3.to_checksum_address(proxy)).call() / 1e6
print(f"Cash balance BEFORE claim: ${balance_before:.2f}")

# Step 2: Get all redeemable positions
print("\nFetching redeemable positions...")
r = requests.get(f"https://data-api.polymarket.com/positions", params={"user": proxy}, timeout=10)

if r.status_code != 200:
    print(f"ERROR: Cannot fetch positions (status {r.status_code})")
    exit(1)

positions = r.json()
redeemable = [p for p in positions if p.get("redeemable", False)]

print(f"Found {len(positions)} total positions")
print(f"Redeemable: {len(redeemable)}")

if not redeemable:
    print("\nNo positions to claim!")
    exit(0)

# Step 3: Claim each redeemable position
print(f"\n{'='*60}")
print("CLAIMING...")
print(f"{'='*60}")

ctf_abi = [
    {"inputs": [{"name": "collateralToken", "type": "address"},
                {"name": "parentCollectionId", "type": "bytes32"},
                {"name": "conditionId", "type": "bytes32"},
                {"name": "indexSets", "type": "uint256[]"}],
     "name": "redeemPositions", "outputs": [],
     "stateMutability": "nonpayable", "type": "function"}
]

custom_proxy_abi = [
    {"inputs": [{"name": "to", "type": "address"}, {"name": "value", "type": "uint256"}, 
                {"name": "data", "type": "bytes"}],
     "name": "execute", "outputs": [{"name": "", "type": "bytes"}],
     "stateMutability": "nonpayable", "type": "function"}
]

ctf = w3.eth.contract(address=Web3.to_checksum_address(CTF_ADDRESS), abi=ctf_abi)
proxy_contract = w3.eth.contract(address=Web3.to_checksum_address(proxy), abi=custom_proxy_abi)
account = Account.from_key(private_key)

claimed_count = 0
total_claimed_value = 0

for pos in redeemable:
    condition_id = pos.get("conditionId")
    value = pos.get("currentValue", 0)
    title = pos.get("title", "Unknown")
    
    print(f"\nClaiming: {title[:50]}")
    print(f"  Value: ${value:.2f}")
    print(f"  Condition: {condition_id[:40]}...")
    
    try:
        # Build redemption calldata
        condition_bytes = Web3.to_bytes(hexstr=condition_id)
        parent_collection = Web3.to_bytes(hexstr="0x" + "00" * 32)
        
        redeem_calldata = ctf.functions.redeemPositions(
            Web3.to_checksum_address(USDC_ADDRESS),
            parent_collection,
            condition_bytes,
            [1, 2]  # Binary: YES, NO
        )._encode_transaction_data()
        
        # Send via proxy.execute()
        nonce = w3.eth.get_transaction_count(account.address)
        
        tx = proxy_contract.functions.execute(
            Web3.to_checksum_address(CTF_ADDRESS),
            0,
            redeem_calldata
        ).build_transaction({
            'from': account.address,
            'nonce': nonce,
            'gas': 400000,
            'gasPrice': w3.eth.gas_price,
            'chainId': 137
        })
        
        signed_tx = account.sign_transaction(tx)
        tx_hash = w3.eth.send_raw_transaction(signed_tx.raw_transaction)
        
        print(f"  TX sent: {tx_hash.hex()[:50]}...")
        print(f"  Waiting for confirmation...")
        
        receipt = w3.eth.wait_for_transaction_receipt(tx_hash, timeout=120)
        
        if receipt['status'] == 1:
            print(f"  SUCCESS! Gas used: {receipt['gasUsed']}")
            claimed_count += 1
            total_claimed_value += value
        else:
            print(f"  FAILED (tx reverted)")
            
    except Exception as e:
        print(f"  ERROR: {str(e)[:80]}")

# Step 4: Check final balance
import time
time.sleep(3)  # Wait for balance to update

balance_after = usdc.functions.balanceOf(Web3.to_checksum_address(proxy)).call() / 1e6

print(f"\n{'='*60}")
print("RESULTS")
print(f"{'='*60}")
print(f"Claimed positions: {claimed_count}/{len(redeemable)}")
print(f"Expected value: ${total_claimed_value:.2f}")
print(f"Cash balance BEFORE: ${balance_before:.2f}")
print(f"Cash balance AFTER:  ${balance_after:.2f}")
print(f"Actual increase: ${balance_after - balance_before:.2f}")
print(f"{'='*60}")

if balance_after > balance_before:
    print("\nSUCCESS! Winnings claimed and cash balance updated!")
    print(f"Ready to trade with ${balance_after:.2f}")
else:
    print("\nWARNING: Balance didn't increase")
    print("Positions might need time to settle or manual claim on website")

