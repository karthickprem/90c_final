"""
Backtest using Polymarket's historical data from Gamma API

Strategy: Test late-entry (last 2 min, 85-95c) across past windows
"""

import json
import requests
import time
from datetime import datetime

GAMMA_API = "https://gamma-api.polymarket.com"

session = requests.Session()


def get_past_windows(num_windows=50):
    """Generate slugs for past N windows"""
    current_ts = int(time.time())
    current_start = current_ts - (current_ts % 900)
    
    windows = []
    for i in range(num_windows):
        start = current_start - (i * 900)  # Go back 15 min each time
        windows.append({
            "slug": f"btc-updown-15m-{start}",
            "start": start,
            "end": start + 900
        })
    
    return windows


def get_market_data(slug):
    """Get market info including outcome"""
    try:
        r = session.get(f"{GAMMA_API}/markets", params={"slug": slug}, timeout=10)
        if r.status_code == 200:
            markets = r.json()
            if markets:
                m = markets[0]
                
                # Get outcome
                outcomes = m.get("outcomes", [])
                outcome_prices = m.get("outcomePrices", [])
                
                if isinstance(outcomes, str):
                    outcomes = json.loads(outcomes)
                if isinstance(outcome_prices, str):
                    outcome_prices = json.loads(outcome_prices)
                
                winner = None
                for o, p in zip(outcomes, outcome_prices):
                    if float(p) >= 0.99:
                        winner = o.lower()
                        break
                
                # Check if we have historical pricing
                # Gamma API provides aggregated stats but not tick-level history
                
                return {
                    "slug": slug,
                    "closed": m.get("closed", False),
                    "winner": winner,
                    "volume": m.get("volume", 0),
                    "liquidity": m.get("liquidity", 0)
                }
    except Exception as e:
        return None
    
    return None


def simulate_late_entry_strategy(windows_data, entry_min=0.85, entry_max=0.95, 
                                 entry_window=120, position_pct=0.35):
    """
    Simulate strategy on historical data
    
    NOTE: Without tick-level historical data from Polymarket's API,
    we need to either:
    1. Use external data providers (DeltaBase, PredictionData.dev)
    2. Collect our own data going forward
    3. Make assumptions about entry opportunities
    
    This function shows the framework structure.
    """
    
    balance = 10.0
    trades = []
    
    print(f"\nTesting: {entry_min*100:.0f}-{entry_max*100:.0f}c, {entry_window}s window, {position_pct*100:.0f}% position")
    print("-" * 60)
    
    for w in windows_data:
        if not w or not w.get("closed") or not w.get("winner"):
            continue  # Skip incomplete data
        
        # Without tick data, we can only check if market CLOSED in our range
        # This is a limitation of Gamma API - it doesn't provide historical ticks
        
        # For now, mark as "no data"
        print(f"  {w['slug']}: closed, winner={w['winner']}, volume=${w['volume']:.0f}")
    
    print("\nLIMITATION: Gamma API doesn't provide tick-level historical data")
    print("We cannot backtest entry timing without tick history")
    print("\nSOLUTIONS:")
    print("1. Use DeltaBase.tech (free 7 days of trades)")
    print("2. Use PredictionData.dev (tick-by-tick replay)")
    print("3. Collect data live with pm_backtest.py --collect")
    
    return {"status": "need_external_data"}


if __name__ == "__main__":
    print("=" * 60)
    print("HISTORICAL DATA CHECK")
    print("=" * 60)
    
    # Check last 20 windows
    print("\nFetching last 20 BTC 15-min windows...")
    windows = get_past_windows(20)
    
    window_data = []
    for i, w in enumerate(windows):
        print(f"\r  Fetching {i+1}/20: {w['slug']}  ", end="", flush=True)
        data = get_market_data(w['slug'])
        if data:
            window_data.append(data)
        time.sleep(0.5)  # Rate limit
    
    print(f"\n\nFetched {len(window_data)} windows")
    print(f"Closed: {len([w for w in window_data if w['closed']])}")
    print(f"With winner: {len([w for w in window_data if w['winner']])}")
    
    print("\n" + "=" * 60)
    print("ISSUE: Gamma API Missing Tick-Level History")
    print("=" * 60)
    print("""
Gamma API provides:
✓ Current prices (live)
✓ Market closed status
✓ Final outcome (winner)

✗ Historical intrawindow prices (tick data)
✗ Price at T-120s, T-90s, etc.

Without tick history, we CANNOT backtest entry timing strategies.

RECOMMENDED SOLUTIONS:
""")
    
    print("\n1. COLLECT LIVE DATA (Best for your strategy)")
    print("   python pm_backtest.py --collect --windows 50")
    print("   Duration: 12.5 hours")
    print("   Gets: Perfect tick data + outcomes")
    
    print("\n2. USE DELTABASE (Fast, free 7 days)")
    print("   Visit: https://deltabase.tech")
    print("   Download: BTC 15-min trades CSV")
    print("   Parse: Extract prices at T-120s for each window")
    
    print("\n3. USE PREDICTIONDATA.DEV (Best quality)")
    print("   Visit: https://predictiondata.dev")
    print("   Get: Tick-by-tick order book reconstruction")
    print("   Cost: Paid service")
    
    print("\n4. RUN LIVE BOT & OPTIMIZE LATER")
    print("   Let pm_fast_bot.py run for 24-48 hours")
    print("   Analyze actual trade results")
    print("   Adjust params based on real performance")
    
    print("\n" + "=" * 60)
    print("RECOMMENDATION: Option 4 (Run live, optimize later)")
    print("Your bot is collecting data AS IT TRADES")
    print("After 20-50 trades, you'll have real results to optimize from")
    print("=" * 60)

