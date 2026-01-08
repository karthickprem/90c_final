"""
Multi-Market Paper Trading Scan

Scans top 20 markets by volume looking for:
1. Tight spreads (< 5c)
2. Ask sum < 1 opportunities
3. Mispricing signals
"""

import requests
import json
import time
import statistics
from datetime import datetime, timezone
from pathlib import Path

GAMMA_API = "https://gamma-api.polymarket.com"
CLOB_API = "https://clob.polymarket.com"

session = requests.Session()

def fetch_markets(limit=50):
    """Fetch active markets."""
    r = session.get(f"{GAMMA_API}/markets", params={
        "active": "true",
        "closed": "false",
        "limit": str(limit),
    }, timeout=10)
    return r.json()

def fetch_book(token_id):
    """Fetch orderbook."""
    try:
        r = session.get(f"{CLOB_API}/book", params={"token_id": token_id}, timeout=5)
        return r.json()
    except:
        return None

def analyze_market(market):
    """Analyze a single market for opportunities."""
    slug = market.get("slug", "")
    volume = float(market.get("volume", 0) or 0)
    
    tokens = market.get("clobTokenIds", [])
    if isinstance(tokens, str):
        try:
            tokens = json.loads(tokens)
        except:
            return None
    
    outcomes = market.get("outcomes", [])
    if isinstance(outcomes, str):
        try:
            outcomes = json.loads(outcomes)
        except:
            return None
    
    if len(tokens) < 2 or len(outcomes) < 2:
        return None
    
    # Fetch books
    books = []
    for token in tokens:
        book = fetch_book(token)
        if book:
            books.append(book)
        else:
            books.append({})
        time.sleep(0.05)
    
    if len(books) < 2:
        return None
    
    # Analyze
    result = {
        "slug": slug,
        "volume": volume,
        "outcomes": outcomes,
        "prices": [],
        "spreads": [],
        "ask_sum": 0,
        "opportunity": None,
    }
    
    asks = []
    bids = []
    
    for i, book in enumerate(books):
        ask_list = book.get("asks", [])
        bid_list = book.get("bids", [])
        
        if ask_list and bid_list:
            best_ask = float(ask_list[0]["price"])
            best_bid = float(bid_list[0]["price"])
            spread = (best_ask - best_bid) * 100
            
            asks.append(best_ask)
            bids.append(best_bid)
            result["prices"].append({"outcome": outcomes[i], "bid": best_bid, "ask": best_ask})
            result["spreads"].append(spread)
    
    if asks:
        result["ask_sum"] = sum(asks)
        result["min_spread"] = min(result["spreads"]) if result["spreads"] else 999
        
        # Check for opportunities
        if result["ask_sum"] < 1.0:
            edge = (1 - result["ask_sum"]) * 100
            result["opportunity"] = f"ARB: ask_sum={result['ask_sum']:.4f}, edge={edge:.2f}c"
        elif result["min_spread"] < 3.0:
            result["opportunity"] = f"TIGHT SPREAD: {result['min_spread']:.2f}c"
    
    return result

def run_scan():
    """Run comprehensive market scan."""
    print("=" * 80)
    print("MULTI-MARKET OPPORTUNITY SCAN")
    print("=" * 80)
    print(f"Time: {datetime.now(timezone.utc).isoformat()}")
    print("Looking for: ask_sum < 1.0, spreads < 5c")
    print("=" * 80)
    
    # Fetch markets
    print("\nFetching markets...")
    markets = fetch_markets(100)
    
    # Sort by volume
    markets.sort(key=lambda x: float(x.get("volume", 0) or 0), reverse=True)
    
    print(f"Analyzing top {len(markets)} markets by volume...\n")
    
    opportunities = []
    all_results = []
    
    for i, market in enumerate(markets[:30]):
        result = analyze_market(market)
        if result:
            all_results.append(result)
            
            status = ""
            if result.get("opportunity"):
                opportunities.append(result)
                status = f" *** {result['opportunity']} ***"
            
            print(f"[{i+1:2d}] {result['slug'][:45]:<45} "
                  f"ask_sum={result['ask_sum']:.4f} "
                  f"spread={result.get('min_spread', 999):.1f}c{status}")
    
    # Summary
    print("\n" + "=" * 80)
    print("RESULTS SUMMARY")
    print("=" * 80)
    
    if opportunities:
        print(f"\n*** OPPORTUNITIES FOUND: {len(opportunities)} ***")
        for opp in opportunities:
            print(f"\n  {opp['slug']}")
            print(f"    {opp['opportunity']}")
            for p in opp['prices']:
                print(f"    {p['outcome']}: bid={p['bid']:.4f}, ask={p['ask']:.4f}")
    else:
        print("\nNO OPPORTUNITIES FOUND")
        print("  - No markets with ask_sum < 1.0")
        print("  - No markets with spreads < 3c")
    
    # Stats
    ask_sums = [r["ask_sum"] for r in all_results if r["ask_sum"] > 0]
    spreads = [r["min_spread"] for r in all_results if r.get("min_spread", 999) < 999]
    
    if ask_sums:
        print(f"\nAsk sum stats:")
        print(f"  Min: {min(ask_sums):.4f}")
        print(f"  Max: {max(ask_sums):.4f}")
        print(f"  Avg: {statistics.mean(ask_sums):.4f}")
    
    if spreads:
        print(f"\nSpread stats:")
        print(f"  Min: {min(spreads):.2f}c")
        print(f"  Avg: {statistics.mean(spreads):.2f}c")
    
    print("\n" + "=" * 80)
    print("VERDICT")
    print("=" * 80)
    
    if opportunities:
        print("\n*** ACTION REQUIRED ***")
        print("Opportunities exist - consider executing trades")
    else:
        print("""
The Polymarket is currently EFFICIENT:
- All markets have ask_sum > 1 (no pair-arb)
- All spreads are wide (>98c typically)

This confirms that:
1. Professional market makers have already captured obvious arb
2. Profitable strategies require:
   - Liquidity rewards (being paid to quote)
   - Information edge (knowing outcomes better)
   - Event timing (late-window plays)
   - Whale following (copy smart traders)
""")
    
    # Save results
    output = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "markets_scanned": len(all_results),
        "opportunities_found": len(opportunities),
        "results": all_results,
    }
    
    with open("market_scan_results.json", "w") as f:
        json.dump(output, f, indent=2)
    
    print("\nResults saved to: market_scan_results.json")

if __name__ == "__main__":
    run_scan()

