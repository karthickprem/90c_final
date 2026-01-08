"""Test order placement directly"""

import os
import sys
import json

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from py_clob_client.client import ClobClient
from py_clob_client.clob_types import OrderArgs, OrderType
from py_clob_client.order_builder.constants import BUY

# Load config
with open("pm_api_config.json") as f:
    cfg = json.load(f)

# Create client (same as pm_fast_bot.py)
client = ClobClient(
    host="https://clob.polymarket.com",
    key=cfg["private_key"],
    chain_id=137,
    signature_type=1,
    funder=cfg["proxy_address"]
)

# Set credentials
from py_clob_client.clob_types import ApiCreds
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
print(f"YES token: {market.yes_token_id[:30]}...")
print(f"NO token: {market.no_token_id[:30]}...")

# Get book using simple approach
import requests

print("\nFetching order books...")
yes_book_raw = requests.get(
    f"https://clob.polymarket.com/book",
    params={"token_id": market.yes_token_id}
).json()
no_book_raw = requests.get(
    f"https://clob.polymarket.com/book",
    params={"token_id": market.no_token_id}
).json()

print(f"YES bids: {len(yes_book_raw.get('bids', []))}")
print(f"YES asks: {len(yes_book_raw.get('asks', []))}")
if yes_book_raw.get('bids'):
    print(f"  Best bid: {yes_book_raw['bids'][0]}")
if yes_book_raw.get('asks'):
    print(f"  Best ask: {yes_book_raw['asks'][0]}")

# Get best bid from book for NO token
no_book_raw = requests.get(
    f"https://clob.polymarket.com/book",
    params={"token_id": market.no_token_id}
).json()

# Find best bid > 0.01
best_bid = 0.01
bids_sorted = sorted(no_book_raw.get('bids', []), key=lambda x: float(x.get("price", 0)), reverse=True)
for bid in bids_sorted:
    price_val = float(bid.get("price", 0))
    if price_val > 0.01:
        best_bid = price_val
        break

print(f"\nNO book best bid (real): {best_bid}")
print(f"NO book all bids sorted: {[(float(b['price']), float(b['size'])) for b in bids_sorted[:5]]}")

# Now try using the clob wrapper
print("\n=== Testing via ClobWrapper ===")
config.mode = RunMode.LIVE
from mm_bot.clob import ClobWrapper
clob = ClobWrapper(config)
book = clob.get_order_book(market.no_token_id)
print(f"ClobWrapper sees: bid={book.best_bid}, ask={book.best_ask}, has_liquidity={book.has_liquidity}")

# Try to place at the ClobWrapper's best_bid
price = book.best_bid
size = 5
token_id = market.no_token_id

print(f"\nTrying via ClobWrapper: BUY {size} NO @ {price:.4f}")

# Test 1: Direct client (like pm_fast_bot.py)
print("\n--- Direct client test ---")
try:
    args = OrderArgs(token_id=token_id, price=price, size=size, side=BUY)
    signed = client.create_order(args)
    print(f"Order signed OK")
    
    result = client.post_order(signed, OrderType.GTC)
    print(f"Result: {result}")
    
    if result and result.get("success"):
        order_id = result.get("orderID")
        print(f"SUCCESS! Order ID: {order_id}")
        
        # Cancel it
        print("Cancelling...")
        client.cancel(order_id)
        print("Cancelled")
    else:
        print(f"FAILED: {result}")

except Exception as e:
    print(f"EXCEPTION: {e}")
    import traceback
    traceback.print_exc()

# Test 2: Via ClobWrapper
print("\n--- ClobWrapper test ---")
try:
    result = clob.post_order(
        token_id=token_id,
        side="BUY",
        price=price,
        size=size,
        post_only=True
    )
    print(f"Result: success={result.success}, order_id={result.order_id}, error={result.error}")
    
    if result.success and result.order_id:
        print(f"SUCCESS!")
        clob.cancel_order(result.order_id)
        print("Cancelled")
    else:
        print(f"FAILED: {result.error}")

except Exception as e:
    print(f"EXCEPTION: {e}")
    import traceback
    traceback.print_exc()

