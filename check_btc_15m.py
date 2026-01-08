"""Check BTC 15m window."""
import requests
import time

ts = int(time.time())
ts = ts - (ts % 900)  # Round to 15 min

slug = f"btc-updown-15m-{ts}"
print(f"Checking: {slug}")

r = requests.get(f"https://gamma-api.polymarket.com/markets?slug={slug}", timeout=10)
print(f"Status: {r.status_code}")
print(f"Response: {r.text[:1000] if r.text else 'empty'}")

# Also try the events endpoint
print("\n\nSearching events for 'btc'...")
r2 = requests.get("https://gamma-api.polymarket.com/events?active=true&limit=200", timeout=10)
events = r2.json()

btc_events = [e for e in events if "btc" in str(e).lower() or "bitcoin" in str(e).lower()]
print(f"Found {len(btc_events)} BTC-related events")

for e in btc_events[:5]:
    print(f"\n{e.get('title', 'no title')}")
    print(f"  Slug: {e.get('slug', 'no slug')}")

