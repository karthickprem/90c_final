"""Quick account status check"""
import os
os.environ["LIVE"] = "1"

from mm_bot.config import Config
from mm_bot.clob import ClobWrapper
import requests

c = Config.from_env()
clob = ClobWrapper(c)

print("=" * 50)
print("ACCOUNT STATUS")
print("=" * 50)

bal = clob.get_balance()
print(f"USDC Balance: ${bal.get('usdc', 0):.2f}")
print(f"Position Value: ${bal.get('positions', 0):.2f}")
print()

print("OPEN ORDERS:")
orders = clob.get_open_orders()
if orders:
    for o in orders:
        token_short = o.asset[:20] if hasattr(o, 'asset') else "unknown"
        print(f"  {o.side} {o.size} @ {o.price}")
else:
    print("  None")
print()

print("ACTIVE POSITIONS:")
r = requests.get(
    "https://data-api.polymarket.com/positions",
    params={"user": c.api.proxy_address},
    timeout=10
)
if r.ok:
    positions = r.json()
    active = [p for p in positions if float(p.get("size", 0)) > 0.01]
    if active:
        for p in active:
            title = p.get("title", "unknown")[:50]
            outcome = p.get("outcome", "?")
            size = float(p.get("size", 0))
            avg = float(p.get("avgPrice", 0))
            val = float(p.get("currentValue", 0))
            print(f"  {outcome}: {size:.2f} shares @ {avg:.4f}")
            print(f"    Market: {title}")
            print(f"    Current Value: ${val:.2f}")
    else:
        print("  None")
else:
    print("  Error fetching")
print()
print("=" * 50)

