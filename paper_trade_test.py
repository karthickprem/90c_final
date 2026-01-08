"""
Simple Paper Trading Test - BTC 15m Markets

Runs for 15-30 minutes, monitors multiple windows, and reports results.
"""

import requests
import json
import time
from datetime import datetime, timezone
from pathlib import Path

GAMMA_API = "https://gamma-api.polymarket.com"
CLOB_API = "https://clob.polymarket.com"

session = requests.Session()

# Results
results = {
    "start_time": None,
    "end_time": None,
    "windows_scanned": 0,
    "ticks_collected": 0,
    "arb_opportunities": 0,
    "trades_executed": 0,
    "total_edge_found_cents": 0,
    "spreads": [],
    "ask_sums": [],
}

# Paper positions
positions = []
pnl = 0.0

def get_window_slug():
    """Get current 15-min window slug."""
    ts = int(time.time())
    ts = ts - (ts % 900)
    return f"btc-updown-15m-{ts}"

def fetch_market(slug):
    """Fetch market by slug."""
    try:
        r = session.get(f"{GAMMA_API}/markets?slug={slug}", timeout=10)
        markets = r.json()
        return markets[0] if markets else None
    except:
        return None

def fetch_book(token_id):
    """Fetch orderbook."""
    try:
        r = session.get(f"{CLOB_API}/book", params={"token_id": token_id}, timeout=5)
        return r.json()
    except:
        return None

def scan_tick(market):
    """Scan one tick and look for opportunities."""
    global results, positions, pnl
    
    tokens = market.get("clobTokenIds", [])
    if isinstance(tokens, str):
        tokens = json.loads(tokens)
    
    if len(tokens) < 2:
        return None
    
    # Fetch both books
    book_up = fetch_book(tokens[0])
    book_down = fetch_book(tokens[1])
    
    if not book_up or not book_down:
        return None
    
    asks_up = book_up.get("asks", [])
    asks_down = book_down.get("asks", [])
    bids_up = book_up.get("bids", [])
    bids_down = book_down.get("bids", [])
    
    if not asks_up or not asks_down or not bids_up or not bids_down:
        return None
    
    ask_up = float(asks_up[0]["price"])
    ask_down = float(asks_down[0]["price"])
    bid_up = float(bids_up[0]["price"])
    bid_down = float(bids_down[0]["price"])
    
    ask_sum = ask_up + ask_down
    spread_up = (ask_up - bid_up) * 100
    spread_down = (ask_down - bid_down) * 100
    
    results["ticks_collected"] += 1
    results["spreads"].append(spread_up)
    results["ask_sums"].append(ask_sum)
    
    tick_data = {
        "ts": time.time(),
        "ask_up": ask_up,
        "ask_down": ask_down,
        "bid_up": bid_up,
        "bid_down": bid_down,
        "ask_sum": ask_sum,
        "spread_up": spread_up,
        "spread_down": spread_down,
    }
    
    # Check for arb
    if ask_sum < 1.0:
        edge = (1 - ask_sum) * 100
        results["arb_opportunities"] += 1
        results["total_edge_found_cents"] += edge
        print(f"\n*** ARB FOUND! Edge: {edge:.2f}c ***")
        print(f"    Buy Up @ {ask_up:.4f} + Buy Down @ {ask_down:.4f} = {ask_sum:.4f}")
        
        # Paper trade
        cost = 10  # $10 per side
        shares_up = cost / ask_up
        shares_down = cost / ask_down
        total_cost = cost * 2
        guaranteed_payout = min(shares_up, shares_down)
        paper_profit = guaranteed_payout - total_cost
        
        positions.append({
            "type": "full_set_arb",
            "cost": total_cost,
            "payout": guaranteed_payout,
            "profit": paper_profit,
        })
        results["trades_executed"] += 1
    
    return tick_data

def run_test(duration_minutes=15):
    """Run paper trading test."""
    global results
    
    print("=" * 70)
    print("PAPER TRADING TEST - BTC 15m Markets")
    print("=" * 70)
    print(f"Duration: {duration_minutes} minutes")
    print(f"Looking for: ask_sum < 1.0 (arbitrage opportunities)")
    print("=" * 70)
    
    results["start_time"] = datetime.now(timezone.utc).isoformat()
    start = time.time()
    deadline = start + duration_minutes * 60
    
    last_slug = None
    tick_count = 0
    
    try:
        while time.time() < deadline:
            elapsed = (time.time() - start) / 60
            
            # Get current window
            slug = get_window_slug()
            
            if slug != last_slug:
                results["windows_scanned"] += 1
                print(f"\n[{elapsed:.1f}m] New window: {slug}")
                last_slug = slug
            
            # Fetch market
            market = fetch_market(slug)
            if not market:
                time.sleep(1)
                continue
            
            # Scan tick
            tick = scan_tick(market)
            tick_count += 1
            
            if tick and tick_count % 20 == 0:
                print(f"  [{elapsed:.1f}m] Tick {tick_count}: ask_sum={tick['ask_sum']:.4f}, "
                      f"spread_up={tick['spread_up']:.1f}c, spread_down={tick['spread_down']:.1f}c")
            
            time.sleep(0.5)  # 500ms between ticks
    
    except KeyboardInterrupt:
        print("\nInterrupted")
    
    results["end_time"] = datetime.now(timezone.utc).isoformat()
    
    # Print results
    print_results()

def print_results():
    """Print final results."""
    print("\n" + "=" * 70)
    print("PAPER TRADING RESULTS")
    print("=" * 70)
    
    duration = (datetime.fromisoformat(results["end_time"].replace("Z", "+00:00")) - 
                datetime.fromisoformat(results["start_time"].replace("Z", "+00:00"))).total_seconds() / 60
    
    print(f"\nDuration: {duration:.1f} minutes")
    print(f"Windows scanned: {results['windows_scanned']}")
    print(f"Ticks collected: {results['ticks_collected']}")
    
    print(f"\n--- OPPORTUNITIES ---")
    print(f"Arb opportunities (ask_sum < 1): {results['arb_opportunities']}")
    print(f"Total edge found: {results['total_edge_found_cents']:.2f} cents")
    
    print(f"\n--- TRADES ---")
    print(f"Trades executed: {results['trades_executed']}")
    
    if positions:
        total_cost = sum(p["cost"] for p in positions)
        total_profit = sum(p["profit"] for p in positions)
        print(f"Total cost: ${total_cost:.2f}")
        print(f"Total profit: ${total_profit:.2f}")
    else:
        print("No trades executed")
    
    print(f"\n--- MARKET STATS ---")
    if results["ask_sums"]:
        import statistics
        avg_ask_sum = statistics.mean(results["ask_sums"])
        min_ask_sum = min(results["ask_sums"])
        max_ask_sum = max(results["ask_sums"])
        print(f"Ask sum - avg: {avg_ask_sum:.4f}, min: {min_ask_sum:.4f}, max: {max_ask_sum:.4f}")
        
        if min_ask_sum < 1.0:
            print(f"*** Minimum ask_sum was {min_ask_sum:.4f} - ARB POSSIBLE! ***")
        else:
            print(f"Minimum ask_sum was {min_ask_sum:.4f} - no arb (need < 1.0)")
    
    if results["spreads"]:
        avg_spread = statistics.mean(results["spreads"])
        min_spread = min(results["spreads"])
        print(f"Spread - avg: {avg_spread:.1f}c, min: {min_spread:.1f}c")
    
    print(f"\n--- VERDICT ---")
    if results["arb_opportunities"] > 0:
        rate = results["arb_opportunities"] / results["ticks_collected"] * 100
        print(f"ARB RATE: {rate:.2f}% of ticks had arb opportunity")
        print("STRATEGY: Viable for pair-arb if you can catch these moments")
    else:
        print("NO ARB FOUND in this period")
        print("Market is too efficient for pair-arb")
        print("Consider: Rewards MM or whale copy trading instead")
    
    # Save results
    output_path = Path("paper_trade_results.json")
    with open(output_path, "w") as f:
        json.dump(results, f, indent=2, default=str)
    print(f"\nResults saved to: {output_path}")

if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument("--duration", type=float, default=15, help="Duration in minutes")
    args = parser.parse_args()
    
    run_test(duration_minutes=args.duration)

