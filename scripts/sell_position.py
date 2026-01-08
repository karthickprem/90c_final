"""Sell the current position to free up cash"""

import os
import sys
import json

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from py_clob_client.client import ClobClient
from py_clob_client.clob_types import ApiCreds, OrderArgs, OrderType
from py_clob_client.order_builder.constants import SELL
import requests

# Load config
with open("pm_api_config.json") as f:
    cfg = json.load(f)

# Create client
client = ClobClient(
    host="https://clob.polymarket.com",
    key=cfg["private_key"],
    chain_id=137,
    signature_type=1,
    funder=cfg["proxy_address"]
)

creds = ApiCreds(
    api_key=cfg["api_key"],
    api_secret=cfg["api_secret"],
    api_passphrase=cfg["api_passphrase"]
)
client.set_api_creds(creds)

# Get current market
from mm_bot.config import Config, RunMode
from mm_bot.market import MarketResolver

config = Config.from_env("pm_api_config.json")
resolver = MarketResolver(config)
market = resolver.resolve_market()

if not market:
    print("Could not resolve market")
    sys.exit(1)

print(f"Market: {market.question}")
print(f"Time left: {market.time_str}")
print(f"NO token: {market.no_token_id}")

# Get NO book
no_book = requests.get(
    f"https://clob.polymarket.com/book",
    params={"token_id": market.no_token_id}
).json()

# Find best bid
best_bid = 0.01
bids_sorted = sorted(no_book.get('bids', []), key=lambda x: float(x.get("price", 0)), reverse=True)
for bid in bids_sorted:
    price_val = float(bid.get("price", 0))
    if price_val > 0.01:
        best_bid = price_val
        break

print(f"Best bid: {best_bid}")

# Get our position
positions = requests.get(
    "https://data-api.polymarket.com/positions",
    params={"user": cfg["proxy_address"]},
    timeout=10
).json()

no_position = None
for p in positions:
    if p.get("asset") == market.no_token_id:
        no_position = p
        break

if not no_position:
    print("No position found in current market")
    sys.exit(0)

shares = float(no_position.get("size", 0))
print(f"Position: {shares:.2f} NO shares")

if shares < 1:
    print("No significant position to sell")
    sys.exit(0)

# Sell at best_bid
sell_price = best_bid
print(f"\nSelling {shares:.2f} NO @ {sell_price:.2f}")

try:
    args = OrderArgs(token_id=market.no_token_id, price=sell_price, size=shares, side=SELL)
    signed = client.create_order(args)
    result = client.post_order(signed, OrderType.GTC)
    
    print(f"Result: {result}")
    
    if result and result.get("success"):
        order_id = result.get("orderID")
        status = result.get("status")
        print(f"Order {status}: {order_id}")
        
        if status == "matched":
            print("SOLD! Cash freed.")
        else:
            print(f"Order placed, waiting for fill...")
    else:
        print(f"FAILED: {result}")

except Exception as e:
    print(f"EXCEPTION: {e}")
    import traceback
    traceback.print_exc()

