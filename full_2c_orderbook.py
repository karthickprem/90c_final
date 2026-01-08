#!/usr/bin/env python3
import requests

token = '86951647963377754570294411064439418450692046913759941345640898022854690525774'
url = f'https://clob.polymarket.com/book?token_id={token}'
r = requests.get(url, timeout=15).json()

asks = sorted(r.get('asks', []), key=lambda x: float(x['price']))
print('ALL asks sorted by price (2C bucket = 35.6-37.4F):')
for a in asks:
    p = a['price']
    s = a['size']
    print(f'  price={p} size={s}')





