"""
SIMPLE 90c BOT - BTC 15min Polymarket

Strategy:
1. Wait until timer < 60 seconds
2. Check which side (Up or Down) has ask <= 90c
3. Buy that side
4. Hold until settlement (timer = 0)
5. Collect $1 if we win

Simple. Clean. Let's see if it works.
"""

import requests
import json
import time
from datetime import datetime, timezone

GAMMA_API = "https://gamma-api.polymarket.com"
CLOB_API = "https://clob.polymarket.com"

session = requests.Session()

# Config
MAX_BUY_PRICE = 0.90  # Buy at 90c or below
TRIGGER_SECONDS = 60  # Buy when < 60 seconds left
TRADE_SIZE = 20.0  # $20 per trade

# State
trades = []
total_pnl = 0.0


def get_window():
    """Get current window info."""
    ts = int(time.time())
    start = ts - (ts % 900)
    end = start + 900
    slug = f"btc-updown-15m-{start}"
    secs_left = end - ts
    
    return {
        "slug": slug,
        "start": start,
        "end": end,
        "secs_left": secs_left,
    }


def get_tokens(slug):
    """Get token IDs for Up and Down."""
    try:
        r = session.get(f"{GAMMA_API}/markets?slug={slug}", timeout=10)
        markets = r.json()
        if not markets:
            return None, None
        
        m = markets[0]
        tokens = m.get("clobTokenIds", [])
        outcomes = m.get("outcomes", [])
        
        if isinstance(tokens, str):
            tokens = json.loads(tokens)
        if isinstance(outcomes, str):
            outcomes = json.loads(outcomes)
        
        token_up = None
        token_down = None
        
        for o, t in zip(outcomes, tokens):
            if str(o).lower() == "up":
                token_up = t
            elif str(o).lower() == "down":
                token_down = t
        
        return token_up, token_down
    except:
        return None, None


def get_best_ask(token_id):
    """Get best ask price and size."""
    try:
        r = session.get(f"{CLOB_API}/book", params={"token_id": token_id}, timeout=5)
        book = r.json()
        asks = book.get("asks", [])
        
        if asks:
            return float(asks[0]["price"]), float(asks[0]["size"])
        return None, None
    except:
        return None, None


def get_btc_price():
    """Get current BTC price."""
    try:
        r = session.get("https://api.coingecko.com/api/v3/simple/price",
                       params={"ids": "bitcoin", "vs_currencies": "usd"}, timeout=10)
        return r.json().get("bitcoin", {}).get("usd", 0)
    except:
        return 0


def run_bot(duration_minutes=30):
    """Run the simple 90c bot."""
    global trades, total_pnl
    
    print("=" * 60)
    print("SIMPLE 90c BOT - BTC 15min")
    print("=" * 60)
    print(f"Strategy: Buy at {MAX_BUY_PRICE*100:.0f}c when < {TRIGGER_SECONDS}s left")
    print(f"Trade size: ${TRADE_SIZE}")
    print(f"Duration: {duration_minutes} minutes")
    print("=" * 60)
    
    start = time.time()
    deadline = start + duration_minutes * 60
    
    last_window = None
    opening_btc = None
    traded_this_window = False
    
    try:
        while time.time() < deadline:
            elapsed = (time.time() - start) / 60
            w = get_window()
            
            # New window?
            if w["slug"] != last_window:
                # Settle previous trades if any
                if last_window and opening_btc:
                    btc_now = get_btc_price()
                    if btc_now:
                        winner = "up" if btc_now >= opening_btc else "down"
                        settle_trades(winner)
                
                print(f"\n[{elapsed:.1f}m] NEW WINDOW: {w['slug']}")
                last_window = w["slug"]
                traded_this_window = False
                opening_btc = None
            
            # Record opening price (first 30 seconds)
            if opening_btc is None and w["secs_left"] < 880:
                opening_btc = get_btc_price()
                if opening_btc:
                    print(f"  Opening BTC: ${opening_btc:,.2f}")
            
            # Check if we should trade
            secs = w["secs_left"]
            
            if secs <= TRIGGER_SECONDS and secs > 5 and not traded_this_window:
                print(f"\n  TRIGGER! {secs:.0f}s left - checking prices...")
                
                token_up, token_down = get_tokens(w["slug"])
                if token_up and token_down:
                    up_ask, up_size = get_best_ask(token_up)
                    down_ask, down_size = get_best_ask(token_down)
                    
                    print(f"    Up ask: {up_ask if up_ask else 'N/A'}")
                    print(f"    Down ask: {down_ask if down_ask else 'N/A'}")
                    
                    # Buy whichever is <= 90c
                    if up_ask and up_ask <= MAX_BUY_PRICE:
                        execute_trade("up", up_ask, w["slug"])
                        traded_this_window = True
                    elif down_ask and down_ask <= MAX_BUY_PRICE:
                        execute_trade("down", down_ask, w["slug"])
                        traded_this_window = True
                    else:
                        print(f"    No side at {MAX_BUY_PRICE*100:.0f}c or below")
            
            # Status update
            if secs > 60:
                print(f"\r  [{secs:.0f}s] Waiting for trigger ({TRIGGER_SECONDS}s)...", end="")
            elif secs > 0:
                print(f"\r  [{secs:.0f}s] In trigger zone - traded: {traded_this_window}", end="")
            
            time.sleep(2)
    
    except KeyboardInterrupt:
        print("\nStopped")
    
    # Final settlement
    if opening_btc:
        btc_now = get_btc_price()
        if btc_now:
            winner = "up" if btc_now >= opening_btc else "down"
            settle_trades(winner)
    
    print_results()


def execute_trade(side, price, slug):
    """Execute a paper trade."""
    global trades
    
    shares = TRADE_SIZE / price
    trades.append({
        "ts": time.time(),
        "slug": slug,
        "side": side,
        "price": price,
        "shares": shares,
        "cost": TRADE_SIZE,
        "status": "open",
        "pnl": 0,
    })
    
    print(f"\n  *** BUY {side.upper()} @ {price:.4f} ***")
    print(f"      Shares: {shares:.2f}")
    print(f"      Cost: ${TRADE_SIZE:.2f}")
    print(f"      If win: ${shares:.2f} profit = ${shares - TRADE_SIZE:.2f}")


def settle_trades(winner):
    """Settle all open trades."""
    global trades, total_pnl
    
    for t in trades:
        if t["status"] == "open":
            if t["side"] == winner:
                # Win: get $1 per share
                t["pnl"] = t["shares"] - t["cost"]
                result = "WIN"
            else:
                # Lose: get $0
                t["pnl"] = -t["cost"]
                result = "LOSS"
            
            t["status"] = "closed"
            total_pnl += t["pnl"]
            
            print(f"\n  SETTLED: {t['side'].upper()} @ {t['price']:.4f}")
            print(f"    Result: {result}")
            print(f"    P&L: ${t['pnl']:.2f}")


def print_results():
    """Print final results."""
    global trades, total_pnl
    
    print("\n" + "=" * 60)
    print("RESULTS")
    print("=" * 60)
    
    print(f"\nTotal trades: {len(trades)}")
    
    closed = [t for t in trades if t["status"] == "closed"]
    if closed:
        wins = [t for t in closed if t["pnl"] > 0]
        losses = [t for t in closed if t["pnl"] <= 0]
        
        print(f"Wins: {len(wins)}")
        print(f"Losses: {len(losses)}")
        
        if closed:
            win_rate = len(wins) / len(closed) * 100
            print(f"Win rate: {win_rate:.1f}%")
    
    open_trades = [t for t in trades if t["status"] == "open"]
    if open_trades:
        print(f"\nOpen trades: {len(open_trades)}")
        for t in open_trades:
            print(f"  {t['side'].upper()} @ {t['price']:.4f}")
    
    print(f"\nTotal P&L: ${total_pnl:.2f}")
    
    if total_pnl > 0:
        print("\n*** PROFITABLE! ***")
    elif total_pnl < 0:
        print("\n*** LOSS ***")
    else:
        print("\n*** BREAK EVEN ***")
    
    # Save results
    results = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "config": {
            "max_buy_price": MAX_BUY_PRICE,
            "trigger_seconds": TRIGGER_SECONDS,
            "trade_size": TRADE_SIZE,
        },
        "total_trades": len(trades),
        "total_pnl": total_pnl,
        "trades": trades,
    }
    
    with open("simple_90c_results.json", "w") as f:
        json.dump(results, f, indent=2)
    
    print("\nResults saved to: simple_90c_results.json")


if __name__ == "__main__":
    import argparse
    p = argparse.ArgumentParser()
    p.add_argument("--duration", type=float, default=30, help="Duration in minutes")
    p.add_argument("--price", type=float, default=0.90, help="Max buy price (0.90 = 90c)")
    p.add_argument("--trigger", type=int, default=60, help="Trigger when < X seconds left")
    args = p.parse_args()
    
    MAX_BUY_PRICE = args.price
    TRIGGER_SECONDS = args.trigger
    
    run_bot(duration_minutes=args.duration)

