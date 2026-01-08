#!/usr/bin/env python3
"""Check if orderbook is consistent across requests."""

import requests
import time

token_id = '53991946850993283973244839140451693673829533119752949187307306094539848989166'
url = f'https://clob.polymarket.com/book?token_id={token_id}'

for i in range(3):
    r = requests.get(url, timeout=15).json()
    asks = r.get("asks", [])
    first_ask = asks[0] if asks else "empty"
    print(f"Request {i+1}: first_ask = {first_ask}")
    time.sleep(0.5)





