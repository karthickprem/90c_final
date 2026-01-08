"""
POLYMARKET OPPORTUNITY SCANNER

Scans all markets for full-set arbitrage (YES + NO < $1.00)
Will BEEP when opportunity found!

Run: python scanner_simple.py
"""
import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry
import time
import sys

sys.stdout.reconfigure(encoding='utf-8')

# ============================================================
# CONFIGURATION
# ============================================================

GAMMA_API = "https://gamma-api.polymarket.com"
MAX_COMBINED = 99  # Alert threshold (cents)
SCAN_INTERVAL = 15  # Seconds between scans

# ============================================================
# HTTP SESSION WITH RETRIES
# ============================================================

def get_session():
    """Create session with retry logic."""
    session = requests.Session()
    retries = Retry(
        total=3,
        backoff_factor=1,
        status_forcelist=[429, 500, 502, 503, 504]
    )
    adapter = HTTPAdapter(max_retries=retries)
    session.mount("https://", adapter)
    session.mount("http://", adapter)
    return session

# ============================================================
# SCANNER
# ============================================================

def scan(session):
    """Scan all markets for opportunities."""
    
    now = time.strftime("%H:%M:%S")
    print(f"\n[{now}] Scanning Polymarket...", flush=True)
    
    try:
        resp = session.get(
            f"{GAMMA_API}/markets",
            params={"closed": "false", "limit": 200},
            timeout=30
        )
        
        if resp.status_code != 200:
            print(f"  API returned {resp.status_code}", flush=True)
            return
        
        markets = resp.json()
        
        # Parse markets
        parsed = []
        for m in markets:
            prices = m.get("outcomePrices", [])
            if len(prices) < 2:
                continue
            
            try:
                yes_mid = float(prices[0]) * 100
                no_mid = float(prices[1]) * 100
                
                # Skip invalid
                if yes_mid <= 0 or no_mid <= 0:
                    continue
                if yes_mid >= 100 or no_mid >= 100:
                    continue
                
                # Estimate ask (mid + 1c spread)
                yes_ask = min(yes_mid + 1, 99)
                no_ask = min(no_mid + 1, 99)
                combined = int(yes_ask + no_ask)
                
                parsed.append({
                    "q": m.get("question", "")[:60],
                    "slug": m.get("slug", ""),
                    "yes": int(yes_ask),
                    "no": int(no_ask),
                    "combined": combined,
                    "edge": 100 - combined,
                    "vol": float(m.get("volume", 0) or 0)
                })
            except:
                continue
        
        # Sort by combined (lowest first = best opportunities)
        parsed.sort(key=lambda x: x["combined"])
        
        print(f"  Parsed {len(parsed)} markets", flush=True)
        
        # Find opportunities
        opportunities = [m for m in parsed if m["combined"] <= MAX_COMBINED]
        
        if opportunities:
            print("\n" + "!" * 60, flush=True)
            print(f"  *** FOUND {len(opportunities)} OPPORTUNITIES! ***", flush=True)
            print("!" * 60, flush=True)
            
            # Beep
            try:
                import winsound
                winsound.Beep(1000, 500)
                winsound.Beep(1500, 500)
            except:
                print("\a" * 3, flush=True)
            
            for opp in opportunities[:5]:
                print(f"\n  EDGE: {opp['edge']}c | Combined: {opp['combined']}c", flush=True)
                print(f"  {opp['q']}", flush=True)
                print(f"  YES: {opp['yes']}c | NO: {opp['no']}c | Vol: ${opp['vol']:,.0f}", flush=True)
                print(f"  https://polymarket.com/event/{opp['slug']}", flush=True)
                
                # Profit estimate
                gross = opp['edge'] / 100 * 10 * 2
                print(f"  Est profit at $10/leg: ${gross - 0.60:.2f}", flush=True)
        else:
            print(f"  No opportunities (all combined > {MAX_COMBINED}c)", flush=True)
            
            # Show closest
            print("\n  Top 5 closest:", flush=True)
            for m in parsed[:5]:
                print(f"    {m['combined']}c | Y:{m['yes']}c N:{m['no']}c | {m['q'][:45]}", flush=True)
        
    except requests.exceptions.ConnectionError as e:
        print(f"  Connection error - check network/VPN", flush=True)
    except Exception as e:
        print(f"  Error: {e}", flush=True)


def main():
    print("=" * 60, flush=True)
    print("POLYMARKET OPPORTUNITY SCANNER", flush=True)
    print("=" * 60, flush=True)
    print(f"Alert threshold: Combined <= {MAX_COMBINED}c", flush=True)
    print(f"Scan interval: {SCAN_INTERVAL}s", flush=True)
    print("Press Ctrl+C to stop", flush=True)
    print("=" * 60, flush=True)
    
    session = get_session()
    
    try:
        while True:
            scan(session)
            time.sleep(SCAN_INTERVAL)
    except KeyboardInterrupt:
        print("\n\nScanner stopped.", flush=True)


if __name__ == "__main__":
    main()
