"""Quick test if Polymarket APIs are accessible"""

import requests

print("Testing Polymarket API access from your network...")
print("=" * 60)

# Test 1: Gamma API
print("\n1. Testing Gamma API (market data)...")
try:
    r = requests.get(
        "https://gamma-api.polymarket.com/markets",
        params={"slug": "btc-updown-15m-1767588300"},
        timeout=10
    )
    print(f"   Status: {r.status_code}")
    
    if r.status_code == 200:
        markets = r.json()
        print(f"   Markets found: {len(markets)}")
        if markets:
            print(f"   Has tokens: {bool(markets[0].get('clobTokenIds'))}")
            print("   [OK] Gamma API accessible")
        else:
            print("   [WARNING] No markets returned")
    else:
        print(f"   [ERROR] HTTP {r.status_code}")
        
except Exception as e:
    print(f"   [BLOCKED] Cannot access: {e}")

# Test 2: CLOB API
print("\n2. Testing CLOB API (price data)...")
try:
    r = requests.get(
        "https://clob.polymarket.com/price",
        params={"token_id": "1234"},  # Dummy ID
        timeout=10
    )
    print(f"   Status: {r.status_code}")
    print("   [OK] CLOB API accessible")
    
except Exception as e:
    print(f"   [BLOCKED] Cannot access: {e}")

# Test 3: Polygon RPC
print("\n3. Testing Polygon RPC (blockchain)...")
try:
    r = requests.post(
        "https://polygon-rpc.com",
        json={"jsonrpc": "2.0", "method": "eth_blockNumber", "params": [], "id": 1},
        timeout=10
    )
    print(f"   Status: {r.status_code}")
    
    if r.status_code == 200:
        result = r.json()
        print(f"   Block: {result.get('result', 'N/A')}")
        print("   [OK] Blockchain accessible")
        
except Exception as e:
    print(f"   [BLOCKED] Cannot access: {e}")

print("\n" + "=" * 60)
print("SUMMARY:")
print("=" * 60)
print("If Gamma/CLOB APIs are blocked:")
print("  -> Use VPN or different network")
print("  -> Bot cannot fetch market data on office network")
print("\nIf Polygon RPC is OK:")
print("  -> Balance reading works fine")
print("  -> Just need Polymarket APIs accessible")

