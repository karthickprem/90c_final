#!/usr/bin/env python3
"""
Raw API check - prints sample questions from Polymarket to verify API access.
"""

import requests
import json

GAMMA_URL = "https://gamma-api.polymarket.com"

def check_api():
    session = requests.Session()
    session.headers.update({
        "User-Agent": "PolymarketDebug/1.0",
        "Accept": "application/json"
    })
    
    print("="*70)
    print("RAW POLYMARKET API CHECK")
    print("="*70)
    
    # Check active markets
    print("\n1. Active markets (/markets?closed=false&active=true):")
    try:
        resp = session.get(f"{GAMMA_URL}/markets", 
                          params={"closed": "false", "active": "true", "limit": 20},
                          timeout=30)
        resp.raise_for_status()
        markets = resp.json()
        print(f"   Got {len(markets)} markets")
        for m in markets[:10]:
            q = m.get("question", "")[:70]
            ob = m.get("enableOrderBook", "?")
            print(f"   - {q}... (OB={ob})")
    except Exception as e:
        print(f"   ERROR: {e}")
    
    # Check closed markets
    print("\n2. Closed markets (/markets?closed=true&active=false):")
    try:
        resp = session.get(f"{GAMMA_URL}/markets", 
                          params={"closed": "true", "active": "false", "limit": 20},
                          timeout=30)
        resp.raise_for_status()
        markets = resp.json()
        print(f"   Got {len(markets)} markets")
        for m in markets[:10]:
            q = m.get("question", "")[:70]
            ob = m.get("enableOrderBook", "?")
            print(f"   - {q}... (OB={ob})")
    except Exception as e:
        print(f"   ERROR: {e}")
    
    # Check events
    print("\n3. Events (/events?closed=false&active=true):")
    try:
        resp = session.get(f"{GAMMA_URL}/events", 
                          params={"closed": "false", "active": "true", "limit": 10},
                          timeout=30)
        resp.raise_for_status()
        events = resp.json()
        print(f"   Got {len(events)} events")
        for e in events[:5]:
            title = e.get("title", "")[:50]
            markets = e.get("markets", [])
            print(f"   - {title}... ({len(markets)} markets)")
    except Exception as e:
        print(f"   ERROR: {e}")
    
    # Search for weather-related using tag or slug
    print("\n4. Looking for weather/temperature in slugs:")
    try:
        resp = session.get(f"{GAMMA_URL}/markets", 
                          params={"closed": "false", "active": "true", "limit": 100},
                          timeout=30)
        resp.raise_for_status()
        markets = resp.json()
        weather_markets = [m for m in markets if any(
            kw in (m.get("slug") or "").lower() 
            for kw in ["weather", "temp", "temperature", "hot", "cold", "degree"]
        )]
        print(f"   Found {len(weather_markets)} with weather-related slugs")
        for m in weather_markets[:10]:
            print(f"   - slug: {m.get('slug')}")
            print(f"     Q: {m.get('question', '')[:60]}")
    except Exception as e:
        print(f"   ERROR: {e}")
    
    # Check API documentation endpoint if available
    print("\n5. Available tags/categories:")
    try:
        # Try to get tags or categories
        resp = session.get(f"{GAMMA_URL}/tags", timeout=10)
        if resp.status_code == 200:
            tags = resp.json()
            print(f"   Got {len(tags) if isinstance(tags, list) else 'unknown'} tags")
            if isinstance(tags, list):
                for t in tags[:20]:
                    print(f"   - {t}")
        else:
            print(f"   Tags endpoint returned {resp.status_code}")
    except Exception as e:
        print(f"   Tags endpoint not available: {e}")
    
    print("\n" + "="*70)
    print("API check complete. If you see markets above, API is working.")
    print("="*70)

if __name__ == "__main__":
    check_api()





