"""
Detect Polymarket wallet type: Gnosis Safe vs Custom Proxy

Two wallet types on Polymarket:
1. Gnosis Safe - uses execTransaction(...)
2. Custom Proxy (Magic Link) - uses execute(...)
"""

import json
from web3 import Web3

RPC_URL = "https://polygon-rpc.com"

# Gnosis Safe interface (partial)
SAFE_ABI = [
    {
        "inputs": [],
        "name": "getOwners",
        "outputs": [{"name": "", "type": "address[]"}],
        "stateMutability": "view",
        "type": "function"
    },
    {
        "inputs": [],
        "name": "getThreshold",
        "outputs": [{"name": "", "type": "uint256"}],
        "stateMutability": "view",
        "type": "function"
    }
]

# Custom proxy interface
CUSTOM_PROXY_ABI = [
    {
        "inputs": [
            {"name": "to", "type": "address"},
            {"name": "value", "type": "uint256"},
            {"name": "data", "type": "bytes"}
        ],
        "name": "execute",
        "outputs": [{"name": "", "type": "bytes"}],
        "stateMutability": "nonpayable",
        "type": "function"
    }
]


def detect_wallet_type(proxy_address: str) -> str:
    """
    Detect if proxy is Gnosis Safe or Custom Proxy
    
    Returns:
        "safe" - Gnosis Safe wallet
        "custom" - Custom Polymarket proxy
        "unknown" - Cannot determine
    """
    w3 = Web3(Web3.HTTPProvider(RPC_URL))
    proxy_addr = Web3.to_checksum_address(proxy_address)
    
    print(f"Detecting wallet type for: {proxy_addr}")
    print()
    
    # Test 1: Try Gnosis Safe methods
    try:
        safe = w3.eth.contract(address=proxy_addr, abi=SAFE_ABI)
        
        owners = safe.functions.getOwners().call()
        threshold = safe.functions.getThreshold().call()
        
        print("GNOSIS SAFE DETECTED!")
        print(f"  Owners: {owners}")
        print(f"  Threshold: {threshold}")
        return "safe"
        
    except Exception as e:
        print(f"Not a Gnosis Safe: {str(e)[:60]}")
    
    print()
    
    # Test 2: Try Custom Proxy execute method
    try:
        # Check if execute selector exists in bytecode
        code = w3.eth.get_code(proxy_addr)
        print(f"Contract code length: {len(code)} bytes")
        
        # execute(address,uint256,bytes) selector = 0xb61d27f6
        execute_selector = Web3.keccak(text="execute(address,uint256,bytes)")[:4]
        print(f"Looking for execute() selector: {execute_selector.hex()}")
        
        if execute_selector in code:
            print("CUSTOM PROXY DETECTED!")
            print(f"  Has execute() method")
            return "custom"
        else:
            print("No execute() method found in bytecode")
            
            # Try to call it anyway to see what happens
            custom = w3.eth.contract(address=proxy_addr, abi=CUSTOM_PROXY_ABI)
            # This might work even if selector not in code (proxy contracts can delegate)
            
    except Exception as e:
        print(f"Custom proxy check failed: {str(e)[:60]}")
    
    print()
    print("WARNING: Could not determine wallet type")
    return "unknown"


def load_config():
    with open("pm_api_config.json") as f:
        return json.load(f)


if __name__ == "__main__":
    config = load_config()
    proxy = config.get("proxy_address")
    
    if not proxy:
        print("Error: No proxy_address in config")
        exit(1)
    
    print("=" * 60)
    print("POLYMARKET WALLET TYPE DETECTION")
    print("=" * 60)
    print()
    
    wallet_type = detect_wallet_type(proxy)
    
    print()
    print("=" * 60)
    print(f"RESULT: {wallet_type.upper()}")
    print("=" * 60)
    
    if wallet_type == "safe":
        print("\nYour wallet is a Gnosis Safe.")
        print("Redemption must use: execTransaction(...)")
        print("Implementation: Safe transaction encoding + signing")
        
    elif wallet_type == "custom":
        print("\nYour wallet is a Custom Polymarket Proxy.")
        print("Redemption can use: execute(...)")
        print("Implementation: Simple execute call (already implemented)")
        
    else:
        print("\nCould not determine wallet type.")
        print("Check proxy address or network connection.")

