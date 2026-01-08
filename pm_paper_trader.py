"""
POLYMARKET PAPER TRADER - BTC 15min Up/Down

Strategy:
- When Up or Down price >= 90c, BUY that side
- Wait for window to close
- Check outcome: if we bought the winning side, WIN!
- Track P&L across multiple windows
"""

import requests
import json
import time
from datetime import datetime

GAMMA_API = "https://gamma-api.polymarket.com"
CLOB_API = "https://clob.polymarket.com"

session = requests.Session()

# Config
ENTRY_THRESHOLD = 0.90  # Enter when price >= 90c
TRADE_SIZE = 10.0  # $10 per trade

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
    mins = int(secs_left // 60)
    secs = int(secs_left % 60)
    return {
        "slug": slug,
        "start": start,
        "end": end,
        "secs_left": secs_left,
        "time_str": f"{mins}:{secs:02d}"
    }


def get_tokens(slug):
    """Get token IDs."""
    try:
        r = session.get(f"{GAMMA_API}/markets", params={"slug": slug}, timeout=10)
        markets = r.json()
        if markets:
            m = markets[0]
            tokens = m.get("clobTokenIds", [])
            outcomes = m.get("outcomes", [])
            if isinstance(tokens, str):
                tokens = json.loads(tokens)
            if isinstance(outcomes, str):
                outcomes = json.loads(outcomes)
            
            result = {}
            for o, t in zip(outcomes, tokens):
                result[o.lower()] = t
            return result
    except:
        pass
    return None


def get_prices(tokens):
    """Get real-time prices using CLOB midpoint."""
    prices = {}
    for side, token in tokens.items():
        try:
            r = session.get(f"{CLOB_API}/midpoint", params={"token_id": token}, timeout=5)
            prices[side] = float(r.json().get("mid", 0))
        except:
            prices[side] = 0
    return prices


def get_outcome(slug):
    """Get outcome of a closed window. Returns 'up', 'down', or None."""
    try:
        r = session.get(f"{GAMMA_API}/markets", params={"slug": slug}, timeout=10)
        markets = r.json()
        if markets:
            m = markets[0]
            if not m.get("closed"):
                return None  # Not closed yet
            
            prices = m.get("outcomePrices", [])
            outcomes = m.get("outcomes", [])
            if isinstance(prices, str):
                prices = json.loads(prices)
            if isinstance(outcomes, str):
                outcomes = json.loads(outcomes)
            
            for o, p in zip(outcomes, prices):
                if float(p) >= 0.99:
                    return o.lower()
    except:
        pass
    return None


def enter_trade(side, price, slug):
    """Enter a paper trade."""
    global trades
    
    shares = TRADE_SIZE / price
    trade = {
        "id": len(trades) + 1,
        "ts": time.time(),
        "slug": slug,
        "side": side,
        "price": price,
        "shares": shares,
        "cost": TRADE_SIZE,
        "status": "open",
        "pnl": 0,
    }
    trades.append(trade)
    
    print(f"\n  *** TRADE #{trade['id']}: BUY {side.upper()} @ {price*100:.0f}c ***", flush=True)
    print(f"      Cost: ${TRADE_SIZE:.2f} | Shares: {shares:.2f}", flush=True)
    print(f"      If win: +${shares - TRADE_SIZE:.2f}", flush=True)
    
    return trade


def settle_trades(slug, winner):
    """Settle all open trades for a window."""
    global trades, total_pnl
    
    settled = 0
    for t in trades:
        if t["status"] == "open" and t["slug"] == slug:
            if t["side"] == winner:
                # Win: get $1 per share
                t["pnl"] = t["shares"] - t["cost"]
                result = "WIN"
            else:
                # Lose: get $0
                t["pnl"] = -t["cost"]
                result = "LOSS"
            
            t["status"] = "closed"
            t["winner"] = winner
            total_pnl += t["pnl"]
            settled += 1
            
            print(f"\n  *** SETTLED #{t['id']}: {result} ***", flush=True)
            print(f"      Bought: {t['side'].upper()} @ {t['price']*100:.0f}c", flush=True)
            print(f"      Winner: {winner.upper()}", flush=True)
            print(f"      P&L: ${t['pnl']:+.2f}", flush=True)
    
    return settled


def run(duration_minutes=60):
    """Run the paper trader."""
    global trades, total_pnl
    
    print("=" * 70, flush=True)
    print("POLYMARKET PAPER TRADER", flush=True)
    print("=" * 70, flush=True)
    print(f"Strategy: BUY when price >= {ENTRY_THRESHOLD*100:.0f}c", flush=True)
    print(f"Trade size: ${TRADE_SIZE:.2f}", flush=True)
    print(f"Duration: {duration_minutes} minutes", flush=True)
    print("=" * 70, flush=True)
    
    start_time = time.time()
    deadline = start_time + duration_minutes * 60
    
    current_slug = None
    tokens = None
    traded_this_window = False
    pending_settlement = None  # slug waiting to be settled
    
    try:
        while time.time() < deadline:
            w = get_window()
            elapsed = (time.time() - start_time) / 60
            
            # New window?
            if w["slug"] != current_slug:
                # Mark previous window for settlement
                if current_slug:
                    pending_settlement = current_slug
                
                print(f"\n[{elapsed:.1f}m] NEW WINDOW: {w['slug']}", flush=True)
                current_slug = w["slug"]
                tokens = get_tokens(current_slug)
                traded_this_window = False
            
            # Check if pending window is settled
            if pending_settlement:
                winner = get_outcome(pending_settlement)
                if winner:
                    print(f"\n[{elapsed:.1f}m] WINDOW CLOSED: {pending_settlement}", flush=True)
                    print(f"  Outcome: {winner.upper()} wins!", flush=True)
                    settle_trades(pending_settlement, winner)
                    pending_settlement = None
            
            # Get current prices
            if tokens:
                prices = get_prices(tokens)
                up_price = prices.get("up", 0)
                down_price = prices.get("down", 0)
                
                up_c = int(up_price * 100)
                down_c = int(down_price * 100)
                
                # Check for entry signal
                if not traded_this_window:
                    if up_price >= ENTRY_THRESHOLD:
                        enter_trade("up", up_price, current_slug)
                        traded_this_window = True
                    elif down_price >= ENTRY_THRESHOLD:
                        enter_trade("down", down_price, current_slug)
                        traded_this_window = True
                
                # Status line
                open_trades = len([t for t in trades if t["status"] == "open"])
                print(f"\r  [{w['time_str']}] Up: {up_c}c | Down: {down_c}c | Open: {open_trades} | P&L: ${total_pnl:+.2f}", end="", flush=True)
            
            time.sleep(2)
    
    except KeyboardInterrupt:
        print("\n\nStopped by user", flush=True)
    
    # Final results
    print_results()


def print_results():
    """Print trading results."""
    global trades, total_pnl
    
    print("\n\n" + "=" * 70, flush=True)
    print("PAPER TRADING RESULTS", flush=True)
    print("=" * 70, flush=True)
    
    closed = [t for t in trades if t["status"] == "closed"]
    open_trades = [t for t in trades if t["status"] == "open"]
    
    print(f"\nTotal trades: {len(trades)}", flush=True)
    print(f"Closed: {len(closed)}", flush=True)
    print(f"Open: {len(open_trades)}", flush=True)
    
    if closed:
        wins = [t for t in closed if t["pnl"] > 0]
        losses = [t for t in closed if t["pnl"] <= 0]
        
        print(f"\nWins: {len(wins)}", flush=True)
        print(f"Losses: {len(losses)}", flush=True)
        
        if closed:
            win_rate = len(wins) / len(closed) * 100
            print(f"Win rate: {win_rate:.1f}%", flush=True)
    
    print(f"\nTotal P&L: ${total_pnl:+.2f}", flush=True)
    
    if total_pnl > 0:
        print("\n*** PROFITABLE! ***", flush=True)
    elif total_pnl < 0:
        print("\n*** LOSS ***", flush=True)
    else:
        print("\n*** BREAK EVEN ***", flush=True)
    
    # Save results
    results = {
        "timestamp": datetime.now().isoformat(),
        "config": {
            "entry_threshold": ENTRY_THRESHOLD,
            "trade_size": TRADE_SIZE,
        },
        "summary": {
            "total_trades": len(trades),
            "wins": len([t for t in closed if t["pnl"] > 0]),
            "losses": len([t for t in closed if t["pnl"] <= 0]),
            "total_pnl": total_pnl,
        },
        "trades": trades,
    }
    
    with open("pm_paper_results.json", "w") as f:
        json.dump(results, f, indent=2)
    
    print("\nResults saved to: pm_paper_results.json", flush=True)


if __name__ == "__main__":
    import argparse
    p = argparse.ArgumentParser()
    p.add_argument("--duration", type=float, default=60, help="Duration in minutes")
    p.add_argument("--threshold", type=float, default=0.90, help="Entry threshold (0.90 = 90c)")
    p.add_argument("--size", type=float, default=10.0, help="Trade size in $")
    args = p.parse_args()
    
    ENTRY_THRESHOLD = args.threshold
    TRADE_SIZE = args.size
    
    run(duration_minutes=args.duration)

