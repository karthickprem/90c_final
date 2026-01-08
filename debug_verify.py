"""Debug verification script"""
import os
import sys
os.environ["LIVE"] = "1"

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from mm_bot.config import Config
from mm_bot.clob import ClobWrapper
from mm_bot.market import MarketResolver
from mm_bot.fill_tracker import FillTracker

print("Loading config...")
config = Config.from_env()
print(f"Config loaded: proxy={config.api.proxy_address[:20]}...")

print("\nCreating CLOB wrapper...")
clob = ClobWrapper(config)
print("CLOB created")

print("\nResolving market...")
resolver = MarketResolver(config)
market = resolver.resolve_market()
if market:
    print(f"Market: {market.question}")
    print(f"YES token: {market.yes_token_id[:30]}...")
    print(f"NO token: {market.no_token_id[:30]}...")
else:
    print("No market found!")
    sys.exit(1)

print("\nGetting order book...")
yes_book = clob.get_order_book(market.yes_token_id)
if yes_book:
    print(f"YES book: bid={yes_book.best_bid:.4f} ask={yes_book.best_ask:.4f}")
    mid = (yes_book.best_bid + yes_book.best_ask) / 2
    print(f"Mid: {mid:.4f}")
else:
    print("No YES book!")

print("\nCreating fill tracker...")
fill_tracker = FillTracker(config)
print("Fill tracker created")

print("\nPolling fills...")
market_tokens = {market.yes_token_id, market.no_token_id}
fills = fill_tracker.poll_fills(market_tokens)
print(f"Found {len(fills)} new fills")

print("\nAll checks passed!")

