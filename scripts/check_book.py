"""Quick book checker"""
import sys
sys.path.insert(0, ".")
import requests
from mm_bot.config import Config
from mm_bot.market import MarketResolver

config = Config.from_env('pm_api_config.json')
resolver = MarketResolver(config)
market = resolver.resolve_market()

if market:
    for side, token in [("YES", market.yes_token_id), ("NO", market.no_token_id)]:
        url = f'https://clob.polymarket.com/book?token_id={token}'
        r = requests.get(url, timeout=10)
        data = r.json()
        
        print(f"\n{side} TOKEN:")
        print(f"  Total bids: {len(data.get('bids', []))}")
        print(f"  Total asks: {len(data.get('asks', []))}")
        
        # Show meaningful levels
        for bid in data.get('bids', []):
            price = float(bid['price'])
            if price > 0.05:
                print(f"    Bid: {price:.2f} x {float(bid['size']):.1f}")
        
        for ask in data.get('asks', []):
            price = float(ask['price'])
            if price < 0.95:
                print(f"    Ask: {price:.2f} x {float(ask['size']):.1f}")
else:
    print("No market")

