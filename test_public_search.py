#!/usr/bin/env python3
"""
Test the correct discovery approach using Gamma public-search endpoint.
"""

import requests
import json

GAMMA_BASE = "https://gamma-api.polymarket.com"

def test_public_search():
    """Test public-search endpoint with temperature queries."""
    
    search_queries = [
        "highest temperature in",
        "Highest temperature in NYC",
        "temperature",
        "highest temperature",
    ]
    
    print("=" * 80)
    print("Testing Gamma public-search endpoint")
    print("=" * 80)
    
    for query in search_queries:
        print(f"\n--- Query: '{query}' ---")
        
        url = f"{GAMMA_BASE}/public-search"
        params = {
            "q": query,
            "events_status": "active",
            "page": 1,
        }
        
        try:
            resp = requests.get(url, params=params, timeout=30)
            print(f"Status: {resp.status_code}")
            print(f"URL: {resp.url}")
            
            if resp.status_code == 200:
                data = resp.json()
                
                # Check structure
                print(f"Response keys: {list(data.keys()) if isinstance(data, dict) else 'list'}")
                
                if isinstance(data, dict):
                    events = data.get("events", [])
                    markets = data.get("markets", [])
                    has_more = data.get("hasMore", False)
                    
                    print(f"Events found: {len(events)}")
                    print(f"Markets found: {len(markets)}")
                    print(f"Has more: {has_more}")
                    
                    # Show first 5 events
                    for i, event in enumerate(events[:5]):
                        title = event.get("title", "NO TITLE")
                        slug = event.get("slug", "NO SLUG")
                        event_id = event.get("id", "NO ID")
                        embedded_markets = event.get("markets", [])
                        print(f"\n  Event {i+1}:")
                        print(f"    ID: {event_id}")
                        print(f"    Title: {title}")
                        print(f"    Slug: {slug}")
                        print(f"    Embedded markets: {len(embedded_markets)}")
                        
                        for j, mkt in enumerate(embedded_markets[:3]):
                            q = mkt.get("question", "NO Q")
                            token_id = mkt.get("clobTokenIds", [])
                            orderbook = mkt.get("enableOrderBook", False)
                            print(f"      Market {j+1}: {q[:80]}...")
                            print(f"        Token IDs: {token_id}, OrderBook: {orderbook}")
                            
                elif isinstance(data, list):
                    print(f"Got list of {len(data)} items")
                    for i, item in enumerate(data[:5]):
                        print(f"  Item {i+1}: {item}")
            else:
                print(f"Error response: {resp.text[:500]}")
                
        except Exception as e:
            print(f"Exception: {e}")
    
    print("\n" + "=" * 80)
    print("Testing /events endpoint (newest first)")
    print("=" * 80)
    
    url = f"{GAMMA_BASE}/events"
    params = {
        "order": "id",
        "ascending": "false",
        "closed": "false",
        "limit": 50,
    }
    
    try:
        resp = requests.get(url, params=params, timeout=30)
        print(f"Status: {resp.status_code}")
        print(f"URL: {resp.url}")
        
        if resp.status_code == 200:
            events = resp.json()
            print(f"Total events returned: {len(events)}")
            
            # Filter for temperature
            temp_events = []
            for event in events:
                title = event.get("title", "").lower()
                if "temperature" in title or "temp" in title:
                    temp_events.append(event)
            
            print(f"Temperature events found: {len(temp_events)}")
            
            for i, event in enumerate(temp_events[:10]):
                title = event.get("title", "NO TITLE")
                slug = event.get("slug", "NO SLUG")
                event_id = event.get("id", "NO ID")
                markets = event.get("markets", [])
                print(f"\n  Temp Event {i+1}:")
                print(f"    ID: {event_id}")
                print(f"    Title: {title}")
                print(f"    Slug: {slug}")
                print(f"    Markets count: {len(markets)}")
                
                for j, mkt in enumerate(markets[:3]):
                    q = mkt.get("question", "NO Q")
                    print(f"      Market: {q[:80]}")
                    
    except Exception as e:
        print(f"Exception: {e}")


if __name__ == "__main__":
    test_public_search()





