"""
BTC 15-MIN EDGE HUNTER

More aggressive strategy that:
1. Monitors for large BTC price movements
2. Calculates true probability using Black-Scholes
3. Looks for ANY price level in the book where edge exists
4. Paper trades immediately when edge appears

The edge appears when:
- BTC moves 0.2%+ from opening -> P(Up) or P(Down) becomes 90%+
- But market still has asks at 85-90c
- That's 5-10c of edge per trade
"""

import requests
import json
import time
import math
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional, List

try:
    from scipy import stats
    norm_cdf = stats.norm.cdf
except:
    def norm_cdf(x):
        a1, a2, a3, a4, a5 = 0.254829592, -0.284496736, 1.421413741, -1.453152027, 1.061405429
        p = 0.3275911
        sign = 1 if x >= 0 else -1
        x = abs(x)
        t = 1.0 / (1.0 + p * x)
        y = 1.0 - (((((a5*t + a4)*t) + a3)*t + a2)*t + a1)*t * math.exp(-x*x/2)
        return 0.5 * (1.0 + sign * y)

GAMMA_API = "https://gamma-api.polymarket.com"
CLOB_API = "https://clob.polymarket.com"
session = requests.Session()


class EdgeHunter:
    def __init__(self, vol: float = 0.25, trade_size: float = 20.0):
        self.vol = vol
        self.trade_size = trade_size
        self.output_dir = Path("edge_hunter_results")
        self.output_dir.mkdir(exist_ok=True)
        
        self.trades = []
        self.total_pnl = 0.0
        self.opening_btc = None
        self.current_window = None
        
    def fetch_btc(self) -> float:
        try:
            r = session.get("https://api.coingecko.com/api/v3/simple/price",
                           params={"ids": "bitcoin", "vs_currencies": "usd"}, timeout=10)
            return r.json().get("bitcoin", {}).get("usd", 0)
        except:
            return 0
    
    def calc_prob(self, current: float, opening: float, secs: float) -> float:
        if opening <= 0 or current <= 0 or secs <= 0:
            return 0.5
        T = secs / (365.25 * 24 * 3600)
        log_ratio = math.log(current / opening)
        vol_sqrt_t = self.vol * math.sqrt(T)
        if vol_sqrt_t < 0.0001:
            return 1.0 if current >= opening else 0.0
        return norm_cdf(log_ratio / vol_sqrt_t)
    
    def get_window(self):
        ts = int(time.time())
        start = ts - (ts % 900)
        end = start + 900
        slug = f"btc-updown-15m-{start}"
        
        tokens = {"up": "", "down": ""}
        try:
            r = session.get(f"{GAMMA_API}/markets?slug={slug}", timeout=10)
            if r.json():
                m = r.json()[0]
                toks = json.loads(m["clobTokenIds"]) if isinstance(m["clobTokenIds"], str) else m["clobTokenIds"]
                outs = json.loads(m["outcomes"]) if isinstance(m["outcomes"], str) else m["outcomes"]
                for o, t in zip(outs, toks):
                    if str(o).lower() == "up": tokens["up"] = t
                    elif str(o).lower() == "down": tokens["down"] = t
        except:
            pass
        
        return {"slug": slug, "start": start, "end": end, "secs": end - ts, "tokens": tokens}
    
    def get_book(self, token: str) -> List[dict]:
        try:
            r = session.get(f"{CLOB_API}/book", params={"token_id": token}, timeout=5)
            return r.json().get("asks", [])
        except:
            return []
    
    def find_edge(self, p_up: float, up_asks: list, down_asks: list) -> Optional[dict]:
        """Find any ask level with positive edge."""
        p_down = 1 - p_up
        
        # Check Up asks
        for ask in up_asks:
            price = float(ask["price"])
            size = float(ask["size"])
            if size < 5:
                continue
            edge = (p_up - price) * 100
            if edge >= 1.0:  # 1 cent minimum edge
                return {"side": "up", "price": price, "size": size, "prob": p_up, "edge": edge}
        
        # Check Down asks
        for ask in down_asks:
            price = float(ask["price"])
            size = float(ask["size"])
            if size < 5:
                continue
            edge = (p_down - price) * 100
            if edge >= 1.0:
                return {"side": "down", "price": price, "size": size, "prob": p_down, "edge": edge}
        
        return None
    
    def trade(self, signal: dict):
        cost = min(self.trade_size, signal["size"])
        self.trades.append({
            "ts": time.time(),
            "side": signal["side"],
            "price": signal["price"],
            "cost": cost,
            "prob": signal["prob"],
            "edge": signal["edge"],
            "status": "open"
        })
        print(f"\n*** TRADE: {signal['side'].upper()} @ {signal['price']:.4f} ***")
        print(f"    P({signal['side']})={signal['prob']*100:.1f}%, Edge={signal['edge']:.1f}c")
    
    def settle(self, winner: str):
        for t in self.trades:
            if t["status"] == "open":
                if t["side"] == winner:
                    shares = t["cost"] / t["price"]
                    pnl = shares - t["cost"]
                else:
                    pnl = -t["cost"]
                t["status"] = "closed"
                t["pnl"] = pnl
                self.total_pnl += pnl
                result = "WIN" if pnl > 0 else "LOSS"
                print(f"\n  SETTLED: {t['side'].upper()} -> {result} ${pnl:.2f}")
    
    def run(self, duration: float = 20):
        print("\n" + "=" * 70)
        print("BTC EDGE HUNTER")
        print("=" * 70)
        print(f"Looking for ANY price level with 1c+ edge")
        print(f"Duration: {duration} minutes")
        print("=" * 70)
        
        start = time.time()
        deadline = start + duration * 60
        last_slug = None
        
        try:
            while time.time() < deadline:
                elapsed = (time.time() - start) / 60
                w = self.get_window()
                
                # New window
                if w["slug"] != last_slug:
                    if last_slug and self.opening_btc:
                        btc = self.fetch_btc()
                        winner = "up" if btc >= self.opening_btc else "down"
                        self.settle(winner)
                    
                    print(f"\n[{elapsed:.1f}m] WINDOW: {w['slug']}")
                    last_slug = w["slug"]
                    self.opening_btc = None
                
                # Record opening
                if not self.opening_btc and w["secs"] < 890:
                    self.opening_btc = self.fetch_btc()
                    if self.opening_btc:
                        print(f"  Opening: ${self.opening_btc:,.2f}")
                
                if not w["tokens"]["up"] or w["secs"] < 30:
                    time.sleep(2)
                    continue
                
                # Get data
                btc = self.fetch_btc()
                up_asks = self.get_book(w["tokens"]["up"])
                down_asks = self.get_book(w["tokens"]["down"])
                
                if btc and self.opening_btc:
                    p_up = self.calc_prob(btc, self.opening_btc, w["secs"])
                    dist = (btc - self.opening_btc) / self.opening_btc * 100
                    
                    # Find edge
                    signal = self.find_edge(p_up, up_asks, down_asks)
                    if signal:
                        # Only trade if we don't have open position
                        open_trades = [t for t in self.trades if t["status"] == "open"]
                        if len(open_trades) < 2:
                            self.trade(signal)
                    
                    # Status
                    best_up = float(up_asks[0]["price"]) if up_asks else 1.0
                    best_down = float(down_asks[0]["price"]) if down_asks else 1.0
                    up_edge = (p_up - best_up) * 100
                    down_edge = ((1-p_up) - best_down) * 100
                    
                    print(f"\r  [{w['secs']:>3.0f}s] BTC ${btc:,.0f} | Dist {dist:+.3f}% | "
                          f"P(Up) {p_up*100:.1f}% | "
                          f"EdgeUp {up_edge:+.1f}c | EdgeDown {down_edge:+.1f}c | "
                          f"Trades {len(self.trades)} | P&L ${self.total_pnl:.2f}", end="")
                
                time.sleep(2)
        
        except KeyboardInterrupt:
            pass
        
        # Final results
        print("\n\n" + "=" * 70)
        print("RESULTS")
        print("=" * 70)
        print(f"Trades: {len(self.trades)}")
        closed = [t for t in self.trades if t["status"] == "closed"]
        if closed:
            wins = [t for t in closed if t.get("pnl", 0) > 0]
            print(f"Wins: {len(wins)}/{len(closed)}")
        print(f"Total P&L: ${self.total_pnl:.2f}")
        
        if self.total_pnl > 0:
            print("\n*** PROFITABLE! ***")
        elif self.total_pnl < 0:
            print("\n*** LOSS ***")
        else:
            print("\n*** BREAK EVEN ***")


if __name__ == "__main__":
    import argparse
    p = argparse.ArgumentParser()
    p.add_argument("--duration", type=float, default=20)
    a = p.parse_args()
    
    EdgeHunter().run(duration=a.duration)

