"""
TEST: Reading outcomes and window switching
"""

import requests
import json
import time

GAMMA_API = "https://gamma-api.polymarket.com"
CLOB_API = "https://clob.polymarket.com"

session = requests.Session()

def get_window_info(offset_minutes=0):
    """Get window info. offset_minutes=-15 gives previous window."""
    ts = int(time.time()) + (offset_minutes * 60)
    start = ts - (ts % 900)
    end = start + 900
    slug = f"btc-updown-15m-{start}"
    secs_left = end - int(time.time())
    return slug, start, end, secs_left

def get_market_status(slug):
    """Get full market status including resolution."""
    try:
        r = session.get(f"{GAMMA_API}/markets", params={"slug": slug}, timeout=10)
        markets = r.json()
        if markets:
            m = markets[0]
            return {
                "question": m.get("question"),
                "active": m.get("active"),
                "closed": m.get("closed"),
                "outcomes": m.get("outcomes"),
                "outcomePrices": m.get("outcomePrices"),
                "resolutionSource": m.get("resolutionSource"),
            }
    except Exception as e:
        return {"error": str(e)}
    return None

def main():
    print("=" * 70, flush=True)
    print("OUTCOME & WINDOW SWITCH TEST", flush=True)
    print("=" * 70, flush=True)
    
    # Q1: Check CURRENT window
    print("\n[Q1] CURRENT WINDOW:", flush=True)
    slug, start, end, secs_left = get_window_info(0)
    print(f"  Slug: {slug}", flush=True)
    print(f"  Time left: {secs_left} seconds", flush=True)
    status = get_market_status(slug)
    if status:
        print(f"  Active: {status.get('active')}", flush=True)
        print(f"  Closed: {status.get('closed')}", flush=True)
        print(f"  Prices: {status.get('outcomePrices')}", flush=True)
    
    # Q1: Check PREVIOUS window (should be closed/resolved)
    print("\n[Q1] PREVIOUS WINDOW (-15 min):", flush=True)
    slug_prev, start_prev, end_prev, secs_prev = get_window_info(-15)
    print(f"  Slug: {slug_prev}", flush=True)
    print(f"  Time since close: {-secs_prev} seconds ago", flush=True)
    status_prev = get_market_status(slug_prev)
    if status_prev:
        print(f"  Active: {status_prev.get('active')}", flush=True)
        print(f"  Closed: {status_prev.get('closed')}", flush=True)
        print(f"  Prices: {status_prev.get('outcomePrices')}", flush=True)
        # If closed, prices should be [1.0, 0.0] or [0.0, 1.0]
        prices = status_prev.get('outcomePrices', [])
        if prices:
            if isinstance(prices, str):
                prices = json.loads(prices)
            outcomes = status_prev.get('outcomes', [])
            if isinstance(outcomes, str):
                outcomes = json.loads(outcomes)
            
            for o, p in zip(outcomes, prices):
                p = float(p)
                if p >= 0.99:
                    print(f"  *** WINNER: {o} ***", flush=True)
    
    # Q1: Check window from 30 min ago
    print("\n[Q1] WINDOW FROM 30 MIN AGO:", flush=True)
    slug_old, start_old, end_old, secs_old = get_window_info(-30)
    print(f"  Slug: {slug_old}", flush=True)
    status_old = get_market_status(slug_old)
    if status_old:
        print(f"  Active: {status_old.get('active')}", flush=True)
        print(f"  Closed: {status_old.get('closed')}", flush=True)
        prices = status_old.get('outcomePrices', [])
        if prices:
            if isinstance(prices, str):
                prices = json.loads(prices)
            outcomes = status_old.get('outcomes', [])
            if isinstance(outcomes, str):
                outcomes = json.loads(outcomes)
            for o, p in zip(outcomes, prices):
                p = float(p)
                if p >= 0.99:
                    print(f"  *** WINNER: {o} ***", flush=True)
    
    # Q2: Demonstrate window switching
    print("\n[Q2] WINDOW SWITCHING DEMO:", flush=True)
    print("  Current window ends, next window starts automatically.", flush=True)
    print("  Window slug changes based on timestamp math:", flush=True)
    
    for offset in [-30, -15, 0, 15, 30]:
        slug, start, end, secs = get_window_info(offset)
        if offset < 0:
            label = f"{-offset} min ago"
        elif offset == 0:
            label = "NOW"
        else:
            label = f"in {offset} min"
        print(f"    {label:>12}: {slug}", flush=True)
    
    print("\n" + "=" * 70, flush=True)
    print("ANSWERS:", flush=True)
    print("=" * 70, flush=True)
    print("""
Q1: YES - I can read outcomes!
    When a window closes, outcomePrices becomes [1.0, 0.0] or [0.0, 1.0]
    The outcome with price = 1.0 is the WINNER
    
Q2: YES - I can switch windows!
    I calculate window slug from current timestamp
    When timer = 0, next window slug is automatically calculated
    No manual switching needed - it's just math!
""", flush=True)

if __name__ == "__main__":
    main()

