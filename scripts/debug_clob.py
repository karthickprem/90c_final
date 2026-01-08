"""Debug ClobWrapper initialization"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from mm_bot.config import Config, RunMode
from mm_bot.clob import ClobWrapper

# Load config and set LIVE mode
config = Config.from_env("pm_api_config.json")
print(f"Initial mode: {config.mode}")
config.mode = RunMode.LIVE
print(f"After setting LIVE: {config.mode}")

# Check config values
print(f"\nConfig values:")
print(f"  api_key: {config.api.api_key[:10]}..." if config.api.api_key else "  api_key: NOT SET")
print(f"  private_key: {config.api.private_key[:10]}..." if config.api.private_key else "  private_key: NOT SET")
print(f"  proxy_address: {config.api.proxy_address}")

# Create ClobWrapper
print(f"\nCreating ClobWrapper...")
clob = ClobWrapper(config)

# Check if client was initialized
print(f"Client initialized: {clob.client is not None}")

if clob.client:
    print(f"Client creds: {clob.client.creds is not None}")
    if clob.client.creds:
        print(f"  api_key: {clob.client.creds.api_key[:10]}...")
    print(f"Client signer: {clob.client.signer is not None}")
    
# Try to get balance
print(f"\nGetting balance...")
bal = clob.get_balance()
print(f"Balance: {bal}")

# Get market
from mm_bot.market import MarketResolver
resolver = MarketResolver(config)
market = resolver.resolve_market()
print(f"\nMarket: {market.question if market else 'NOT FOUND'}")

if market:
    # Try to place an order
    print(f"\nPlacing test order...")
    result = clob.post_order(
        token_id=market.no_token_id,
        side="BUY",
        price=0.10,  # Very cheap
        size=5,
        post_only=True
    )
    print(f"Result: {result}")
    
    if result.success and result.order_id:
        print(f"SUCCESS! Cancelling...")
        clob.cancel_order(result.order_id)
        print("Cancelled")

