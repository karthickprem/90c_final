"""Quick script to test Polymarket API discovery."""

import asyncio
import aiohttp
import time

async def test_api():
    async with aiohttp.ClientSession() as session:
        # Test 1: Try the constructed slug
        ts = int(time.time())
        start = ts - (ts % 900)
        slug = f'btc-updown-15m-{start}'
        
        print(f'Testing slug: {slug}')
        async with session.get('https://gamma-api.polymarket.com/markets', params={'slug': slug}) as resp:
            data = await resp.json()
            print(f'  Result: {len(data)} markets found')
        
        # Test 2: Search for active markets
        print(f'\nSearching for ALL active markets...')
        async with session.get('https://gamma-api.polymarket.com/markets', params={'closed': 'false', 'active': 'true', 'limit': '200'}) as resp:
            markets = await resp.json()
            print(f'  Total active markets: {len(markets)}')
            
            # Look for BTC/crypto markets
            btc_markets = []
            for m in markets:
                q = m.get('question', '').lower()
                s = m.get('slug', '').lower()
                if 'btc' in q or 'btc' in s or 'bitcoin' in q or 'bitcoin' in s:
                    btc_markets.append(m)
            
            print(f'  BTC-related markets: {len(btc_markets)}')
            for m in btc_markets[:20]:
                slug = m.get('slug', '?')[:60]
                question = m.get('question', '?')[:80]
                print(f'    - {slug}')
                print(f'      Q: {question}')
        
        # Test 3: Look for any "15" related markets
        print(f'\nSearching for 15-min style markets...')
        min15_markets = [m for m in markets if '15' in m.get('slug', '') or '15' in m.get('question', '')]
        print(f'  Markets with "15" in slug/question: {len(min15_markets)}')
        for m in min15_markets[:10]:
            slug = m.get('slug', '?')[:60]
            print(f'    - {slug}')

if __name__ == '__main__':
    asyncio.run(test_api())


