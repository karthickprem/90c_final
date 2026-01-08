#!/usr/bin/env python3
"""Print ALL asks for a token."""

import requests

token_id = '19739385386431242025879082628609103708781347913200047636484592475750159654048'
url = f'https://clob.polymarket.com/book?token_id={token_id}'
r = requests.get(url, timeout=15).json()
asks = r.get('asks', [])

print(f'Total asks: {len(asks)}')
print('ALL asks sorted by price:')
asks_sorted = sorted(asks, key=lambda x: float(x['price']))
for a in asks_sorted:
    price = a['price']
    size = a['size']
    print(f'  price={price} size={size}')





