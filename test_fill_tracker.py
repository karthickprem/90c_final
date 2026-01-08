"""Test fill tracker with fixed API parsing"""
import os
os.environ["LIVE"] = "1"

from mm_bot.config import Config
from mm_bot.fill_tracker import FillTracker

c = Config.from_env()

# Create fill tracker (takes config object)
ft = FillTracker(c)

# Get a recent market token (use the one from our recent trades)
test_token = "3957328916472580780667178029148259601214040478271702240235922259557882788921"

print("Testing fill tracker...")
print(f"Token: {test_token[:30]}...")

# Poll for fills
fills = ft.poll_fills({test_token})

print(f"\nDetected {len(fills)} fills:")
for f in fills:
    print(f"  {f.side} {f.size:.2f} @ {f.price:.4f}")
    print(f"    trade_id: {f.trade_id[:40]}...")
    print(f"    source: {f.source}")
    print(f"    timestamp: {f.timestamp}")
    print()

print("Fill tracker test complete!")

