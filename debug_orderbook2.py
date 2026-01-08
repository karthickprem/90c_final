#!/usr/bin/env python3
"""Debug script to find the orderbook parsing bug."""

import requests
import json
from bot.gamma import GammaClient

def main():
    g = GammaClient()
    markets = g.discover_bucket_markets(locations=['london'])
    
    # Get one active market
    active = [m for m in markets if not m.closed and m.target_date.day == 4]
    middle = [m for m in active if m.tmin_f > 33 and m.tmax_f < 36]
    
    if not middle:
        print("No matching bucket")
        return
    
    m = middle[0]
    print(f"Question: {m.question}")
    print(f"Market ID: {m.market_id}")
    print(f"YES token (from TemperatureMarket): {m.yes_token_id}")
    print(f"NO token (from TemperatureMarket): {m.no_token_id}")
    print()
    
    # Raw call with the YES token
    yes_token = m.yes_token_id
    no_token = m.no_token_id
    
    print("="*60)
    print("YES TOKEN ORDERBOOK")
    print("="*60)
    url = f"https://clob.polymarket.com/book?token_id={yes_token}"
    resp = requests.get(url, timeout=15)
    if resp.status_code == 200:
        data = resp.json()
        print(f"Asks (first 5, raw):")
        for a in data.get("asks", [])[:5]:
            print(f"  price={a['price']} size={a['size']}")
        print(f"Bids (first 5, raw):")
        for b in data.get("bids", [])[:5]:
            print(f"  price={b['price']} size={b['size']}")
    else:
        print(f"Error: {resp.status_code}")
    
    print()
    print("="*60)
    print("NO TOKEN ORDERBOOK")
    print("="*60)
    if no_token:
        url = f"https://clob.polymarket.com/book?token_id={no_token}"
        resp = requests.get(url, timeout=15)
        if resp.status_code == 200:
            data = resp.json()
            print(f"Asks (first 5, raw):")
            for a in data.get("asks", [])[:5]:
                print(f"  price={a['price']} size={a['size']}")
            print(f"Bids (first 5, raw):")
            for b in data.get("bids", [])[:5]:
                print(f"  price={b['price']} size={b['size']}")
        else:
            print(f"Error: {resp.status_code}")
    
    # Now check what CLOBClient does
    print()
    print("="*60)
    print("CLOBClient.get_book() RESULT")
    print("="*60)
    from bot.clob import CLOBClient
    clob = CLOBClient()
    
    print(f"Calling get_book('{yes_token[:30]}...')")
    book = clob.get_book(yes_token)
    print(f"Returned asks: {book.asks[:5]}")
    print(f"Returned bids: {book.bids[:5]}")


if __name__ == "__main__":
    main()





