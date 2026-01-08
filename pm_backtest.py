"""
Backtesting Framework for Late-Entry Strategy

Collects historical tick data and tests different entry parameters:
- Entry price range (85-95c, 80-90c, etc.)
- Entry time window (1min, 2min, 3min)
- Position size (25%, 35%, 50%)
"""

import json
import time
import requests
from datetime import datetime
from typing import List, Dict

GAMMA_API = "https://gamma-api.polymarket.com"
CLOB_API = "https://clob.polymarket.com"

session = requests.Session()


class TickCollector:
    """Collect tick-level price data for backtesting"""
    
    def __init__(self):
        self.windows = []
    
    def get_window(self, offset_minutes=0):
        """Get window info"""
        ts = int(time.time()) + (offset_minutes * 60)
        start = ts - (ts % 900)
        end = start + 900
        return {
            "slug": f"btc-updown-15m-{start}",
            "start": start,
            "end": end,
            "secs_left": end - ts
        }
    
    def get_tokens(self, slug):
        """Get token IDs"""
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
                return {o.lower(): t for o, t in zip(outcomes, tokens)}
        except:
            pass
        return {}
    
    def get_prices(self, tokens):
        """Get current prices"""
        prices = {}
        for side, token in tokens.items():
            try:
                r = session.get(f"{CLOB_API}/midpoint", params={"token_id": token}, timeout=3)
                if r.status_code == 200:
                    prices[side] = float(r.json().get("mid", 0))
                else:
                    prices[side] = 0
            except:
                prices[side] = 0
        return prices
    
    def get_outcome(self, slug):
        """Get outcome after window closes"""
        try:
            r = session.get(f"{GAMMA_API}/markets", params={"slug": slug}, timeout=10)
            markets = r.json()
            if markets:
                m = markets[0]
                if not m.get("closed"):
                    return None
                
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
    
    def collect_window(self, window_info):
        """Collect tick data for one complete window"""
        print(f"\nCollecting: {window_info['slug']}")
        print(f"Time left: {window_info['secs_left']}s")
        
        tokens = self.get_tokens(window_info['slug'])
        if not tokens:
            print("  ERROR: No tokens")
            return None
        
        ticks = []
        start_time = time.time()
        
        # Collect ticks every 1 second until window ends
        while window_info['secs_left'] > 0:
            tick_time = time.time()
            prices = self.get_prices(tokens)
            
            tick = {
                "ts": tick_time,
                "secs_left": window_info['secs_left'],
                "up": prices.get("up", 0),
                "down": prices.get("down", 0)
            }
            ticks.append(tick)
            
            # Update display
            print(f"\r  [{window_info['secs_left']}s] UP={prices.get('up', 0)*100:.0f}c DOWN={prices.get('down', 0)*100:.0f}c | Ticks: {len(ticks)}  ", end="", flush=True)
            
            time.sleep(1)
            
            # Update window info
            window_info = self.get_window()
        
        print(f"\n  Window ended - collected {len(ticks)} ticks")
        
        # Wait for outcome
        print("  Waiting for outcome...")
        outcome = None
        for i in range(60):
            outcome = self.get_outcome(window_info['slug'])
            if outcome:
                break
            time.sleep(2)
        
        if outcome:
            print(f"  Outcome: {outcome.upper()} wins")
        else:
            print("  Could not get outcome")
        
        return {
            "slug": window_info['slug'],
            "start": window_info['start'],
            "end": window_info['end'],
            "ticks": ticks,
            "outcome": outcome,
            "collected_at": datetime.now().isoformat()
        }
    
    def collect_multiple_windows(self, num_windows=20):
        """Collect data for multiple windows"""
        print("=" * 60)
        print("TICK DATA COLLECTOR")
        print("=" * 60)
        print(f"Collecting {num_windows} windows for backtesting")
        print("=" * 60)
        
        collected = []
        
        for i in range(num_windows):
            w = self.get_window()
            
            # Wait until near end of current window to collect full data
            if w['secs_left'] > 60:
                wait = w['secs_left'] - 55
                print(f"\nWaiting {wait}s for window {i+1}/{num_windows} to start...")
                time.sleep(wait)
                w = self.get_window()
            
            window_data = self.collect_window(w)
            if window_data:
                collected.append(window_data)
                
                # Save incrementally
                filename = f"backtest_data_{datetime.now().strftime('%Y%m%d_%H%M%S')}.json"
                with open(filename, 'w') as f:
                    json.dump(collected, f, indent=2)
                
                print(f"  Saved to {filename}")
        
        print(f"\n{'='*60}")
        print(f"Collection complete: {len(collected)} windows")
        print(f"{'='*60}")
        
        return collected


class Backtester:
    """Test different strategies on historical data"""
    
    def __init__(self, data_file):
        with open(data_file) as f:
            self.windows = json.load(f)
        print(f"Loaded {len(self.windows)} windows from {data_file}")
    
    def test_strategy(self, entry_min, entry_max, entry_window_secs, position_pct):
        """
        Simulate trading strategy
        
        Args:
            entry_min: Minimum entry price (e.g., 0.85)
            entry_max: Maximum entry price (e.g., 0.95)
            entry_window_secs: Only trade in last N seconds
            position_pct: Position size as fraction of balance
        """
        balance = 10.0  # Start with $10
        trades = []
        
        for window in self.windows:
            if not window.get("outcome"):
                continue  # Skip if no outcome
            
            ticks = window["ticks"]
            outcome = window["outcome"]
            
            # Find entry opportunity in the entry window
            entry = None
            for tick in ticks:
                secs_left = tick["secs_left"]
                
                # Check if in entry window (e.g., last 120s to 30s)
                if 30 <= secs_left <= entry_window_secs:
                    up = tick["up"]
                    down = tick["down"]
                    
                    # Check if price in range
                    if entry_min <= up <= entry_max:
                        entry = {"side": "up", "price": up, "secs_left": secs_left}
                        break
                    elif entry_min <= down <= entry_max:
                        entry = {"side": "down", "price": down, "secs_left": secs_left}
                        break
            
            if entry:
                # Simulate trade
                use = balance * position_pct
                shares = use / entry["price"]
                
                if shares >= 5:  # Meet minimum
                    won = (entry["side"] == outcome)
                    
                    if won:
                        payout = shares * 1.0
                        profit = payout - use
                        balance += profit
                    else:
                        balance -= use
                    
                    trades.append({
                        "slug": window["slug"],
                        "side": entry["side"],
                        "price": entry["price"],
                        "secs_left": entry["secs_left"],
                        "shares": shares,
                        "cost": use,
                        "outcome": outcome,
                        "won": won,
                        "pnl": profit if won else -use,
                        "balance_after": balance
                    })
        
        # Calculate stats
        wins = [t for t in trades if t["won"]]
        losses = [t for t in trades if not t["won"]]
        
        total_pnl = balance - 10.0
        win_rate = len(wins) / len(trades) if trades else 0
        
        return {
            "config": {
                "entry_min": entry_min,
                "entry_max": entry_max,
                "entry_window_secs": entry_window_secs,
                "position_pct": position_pct
            },
            "results": {
                "total_trades": len(trades),
                "wins": len(wins),
                "losses": len(losses),
                "win_rate": win_rate,
                "starting_balance": 10.0,
                "ending_balance": balance,
                "total_pnl": total_pnl,
                "roi": total_pnl / 10.0 * 100
            },
            "trades": trades
        }
    
    def run_parameter_sweep(self):
        """Test multiple parameter combinations"""
        print("\n" + "=" * 60)
        print("PARAMETER SWEEP")
        print("=" * 60)
        
        configs = [
            # (entry_min, entry_max, window_secs, position%)
            (0.85, 0.95, 120, 0.35),  # Current
            (0.85, 0.95, 180, 0.35),  # 3 min window
            (0.85, 0.95, 90, 0.35),   # 1.5 min window
            (0.85, 0.95, 60, 0.35),   # 1 min window
            (0.80, 0.90, 120, 0.35),  # Lower range
            (0.90, 0.98, 120, 0.35),  # Higher range
            (0.85, 0.95, 120, 0.50),  # Larger position
            (0.85, 0.95, 120, 0.25),  # Smaller position
        ]
        
        results = []
        
        for entry_min, entry_max, window, pos in configs:
            result = self.test_strategy(entry_min, entry_max, window, pos)
            results.append(result)
            
            print(f"\n{entry_min*100:.0f}-{entry_max*100:.0f}c | {window}s window | {pos*100:.0f}% position:")
            print(f"  Trades: {result['results']['total_trades']}")
            print(f"  Win rate: {result['results']['win_rate']*100:.1f}%")
            print(f"  P&L: ${result['results']['total_pnl']:+.2f} ({result['results']['roi']:+.1f}%)")
        
        # Save results
        filename = f"backtest_results_{datetime.now().strftime('%Y%m%d_%H%M%S')}.json"
        with open(filename, 'w') as f:
            json.dump(results, f, indent=2)
        
        print(f"\n{'='*60}")
        print(f"Results saved to: {filename}")
        print(f"{'='*60}")
        
        # Find best config
        best = max(results, key=lambda x: x['results']['total_pnl'])
        print(f"\nBEST CONFIG:")
        print(f"  Entry: {best['config']['entry_min']*100:.0f}-{best['config']['entry_max']*100:.0f}c")
        print(f"  Window: {best['config']['entry_window_secs']}s")
        print(f"  Position: {best['config']['position_pct']*100:.0f}%")
        print(f"  P&L: ${best['results']['total_pnl']:+.2f}")
        print(f"  Win Rate: {best['results']['win_rate']*100:.1f}%")


if __name__ == "__main__":
    import argparse
    
    p = argparse.ArgumentParser()
    p.add_argument("--collect", action="store_true", help="Collect new tick data")
    p.add_argument("--windows", type=int, default=20, help="Number of windows to collect")
    p.add_argument("--backtest", type=str, help="Backtest using data file")
    args = p.parse_args()
    
    if args.collect:
        collector = TickCollector()
        collector.collect_multiple_windows(args.windows)
    
    elif args.backtest:
        tester = Backtester(args.backtest)
        tester.run_parameter_sweep()
    
    else:
        print("Usage:")
        print("  Collect data:  python pm_backtest.py --collect --windows 20")
        print("  Run backtest:  python pm_backtest.py --backtest backtest_data_*.json")

