"""Test to find correct portfolio balance endpoint"""

import requests

proxy = "0x3C008F983c1d1097a1304e38B683B018aC589500"

print("Testing Polymarket Data API endpoints...")
print("=" * 60)

# Test different endpoints
tests = [
    ("Value", f"https://data-api.polymarket.com/value?user={proxy}"),
    ("Portfolio", f"https://data-api.polymarket.com/portfolio?user={proxy}"),
    ("User", f"https://data-api.polymarket.com/user?address={proxy}"),
    ("Positions", f"https://data-api.polymarket.com/positions?user={proxy}"),
]

for name, url in tests:
    try:
        r = requests.get(url, timeout=5)
        print(f"\n{name}: {url}")
        print(f"  Status: {r.status_code}")
        if r.status_code == 200:
            data = r.json()
            print(f"  Response type: {type(data)}")
            if isinstance(data, list):
                print(f"  Length: {len(data)}")
                if data:
                    print(f"  First item: {data[0]}")
            elif isinstance(data, dict):
                print(f"  Keys: {list(data.keys())}")
                print(f"  Data: {data}")
    except Exception as e:
        print(f"\n{name}: ERROR - {e}")

print("\n" + "=" * 60)
print("Looking for: Total portfolio value = $8.24")
print("(Should be cash $2.19 + unredeemed positions $6.05)")
print("=" * 60)

