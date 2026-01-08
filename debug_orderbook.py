#!/usr/bin/env python3
"""Debug script to check raw orderbook data."""

import requests
import json
from bot.gamma import GammaClient
from bot.clob import CLOBClient

def main():
    g = GammaClient()
    markets = g.discover_bucket_markets(locations=['london'])
    
    # Get active markets only (Jan 4 or 5)
    active = [m for m in markets if not m.closed and m.target_date.day in [4, 5]]
    print(f"Active London markets: {len(active)}")
    
    # Get a middle bucket (not tail)
    middle = [m for m in active if m.tmin_f > 30 and m.tmax_f < 40]
    
    if not middle:
        print("No middle buckets found")
        return
    
    for m in middle[:3]:
        token_id = m.yes_token_id
        print(f"\n{'='*60}")
        print(f"Token: {token_id[:40]}...")
        print(f"Question: {m.question}")
        print(f"Parsed bucket: {m.tmin_f:.1f}-{m.tmax_f:.1f}F (orig unit: {m.temp_unit})")
        print(f"Date: {m.target_date}")
        print()
        
        # Raw API call
        url = f"https://clob.polymarket.com/book?token_id={token_id}"
        try:
            resp = requests.get(url, timeout=15)
            resp.raise_for_status()
            data = resp.json()
            
            print("RAW ASKS (first 5):")
            asks = data.get("asks", [])
            for a in asks[:5]:
                print(f"  price={a['price']} size={a['size']}")
            if not asks:
                print("  (empty)")
            
            print("\nRAW BIDS (first 5):")
            bids = data.get("bids", [])
            for b in bids[:5]:
                print(f"  price={b['price']} size={b['size']}")
            if not bids:
                print("  (empty)")
            
            # Now test my CLOB client
            print("\n--- My CLOB Client ---")
            clob = CLOBClient()
            book = clob.get_book(token_id)
            print(f"Parsed asks: {book.asks[:3] if book else 'None'}")
            print(f"Parsed bids: {book.bids[:3] if book else 'None'}")
            
            best_ask = clob.best_ask_price(token_id)
            print(f"best_ask_price(): {best_ask}")
            
            fill = clob.fill_cost_for_shares(token_id, 10)
            print(f"fill_cost_for_shares(10): avg_price={fill.avg_price:.4f}, total_cost={fill.total_cost:.4f}, filled={fill.filled_shares}")
            
        except Exception as e:
            print(f"Error: {e}")


if __name__ == "__main__":
    main()





