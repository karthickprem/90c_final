"""
BTC 15-MIN SMART TRADER

Improved strategy based on orderbook analysis:
1. Look at deeper book levels (not just best bid/ask)
2. Calculate true probability from BTC price distance
3. Find edge when market prices differ from true probability
4. Paper trade at available price levels

KEY INSIGHT:
The books have liquidity at 0.85-0.90 levels
If true probability is >90%, buying at 0.85 is profitable!
"""

import requests
import json
import time
import math
import statistics
from datetime import datetime, timezone
from pathlib import Path
from dataclasses import dataclass
from typing import List, Optional, Tuple

try:
    from scipy import stats
    def norm_cdf(x):
        return stats.norm.cdf(x)
except ImportError:
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


@dataclass
class BookLevel:
    price: float
    size: float


@dataclass
class Trade:
    ts: float
    side: str  # "up" or "down"
    entry_price: float
    size_usd: float
    true_prob: float
    edge_cents: float
    exit_price: float = 0
    pnl: float = 0
    status: str = "open"


class BTCSmartTrader:
    def __init__(
        self,
        annual_volatility: float = 0.25,
        min_edge_cents: float = 3.0,  # Need at least 3c edge
        trade_size: float = 20.0,
        max_price: float = 0.92,  # Don't pay more than 92c
        output_dir: str = "btc_smart_results",
    ):
        self.annual_vol = annual_volatility
        self.min_edge = min_edge_cents
        self.trade_size = trade_size
        self.max_price = max_price
        
        self.output_dir = Path(output_dir)
        self.output_dir.mkdir(exist_ok=True)
        
        self.trades: List[Trade] = []
        self.total_pnl = 0.0
        self.opening_price: Optional[float] = None
        
        ts_str = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
        self.log_path = self.output_dir / f"smart_{ts_str}.jsonl"
    
    def fetch_btc(self) -> float:
        """Fetch current BTC price."""
        try:
            r = session.get(
                "https://api.coingecko.com/api/v3/simple/price",
                params={"ids": "bitcoin", "vs_currencies": "usd"},
                timeout=10
            )
            return r.json().get("bitcoin", {}).get("usd", 0)
        except:
            return 0
    
    def fetch_book(self, token_id: str) -> Tuple[List[BookLevel], List[BookLevel]]:
        """Fetch full orderbook."""
        try:
            r = session.get(f"{CLOB_API}/book", params={"token_id": token_id}, timeout=5)
            book = r.json()
            
            bids = [BookLevel(float(b["price"]), float(b["size"])) for b in book.get("bids", [])]
            asks = [BookLevel(float(a["price"]), float(a["size"])) for a in book.get("asks", [])]
            
            return bids, asks
        except:
            return [], []
    
    def get_window_info(self) -> dict:
        """Get current window info."""
        ts = int(time.time())
        window_start = ts - (ts % 900)
        window_end = window_start + 900
        slug = f"btc-updown-15m-{window_start}"
        
        info = {
            "slug": slug,
            "start_ts": window_start,
            "end_ts": window_end,
            "seconds_remaining": window_end - ts,
            "token_up": "",
            "token_down": "",
        }
        
        try:
            r = session.get(f"{GAMMA_API}/markets?slug={slug}", timeout=10)
            markets = r.json()
            if markets:
                market = markets[0]
                tokens = market.get("clobTokenIds", [])
                if isinstance(tokens, str):
                    tokens = json.loads(tokens)
                outcomes = market.get("outcomes", [])
                if isinstance(outcomes, str):
                    outcomes = json.loads(outcomes)
                
                for out, tok in zip(outcomes, tokens):
                    if str(out).lower() == "up":
                        info["token_up"] = tok
                    elif str(out).lower() == "down":
                        info["token_down"] = tok
        except:
            pass
        
        return info
    
    def calc_probability(self, current: float, opening: float, secs_remaining: float) -> float:
        """Calculate P(Up) using Black-Scholes."""
        if opening <= 0 or current <= 0:
            return 0.5
        if secs_remaining <= 0:
            return 1.0 if current >= opening else 0.0
        
        T = secs_remaining / (365.25 * 24 * 3600)
        log_ratio = math.log(current / opening)
        vol_sqrt_t = self.annual_vol * math.sqrt(T)
        
        if vol_sqrt_t < 0.0001:
            return 1.0 if current >= opening else 0.0
        
        d2 = log_ratio / vol_sqrt_t
        return norm_cdf(d2)
    
    def find_best_ask(self, asks: List[BookLevel], max_price: float) -> Optional[BookLevel]:
        """Find the best ask at or below max_price."""
        for ask in asks:
            if ask.price <= max_price and ask.size >= 10:  # Min $10 size
                return ask
        return None
    
    def check_signal(
        self,
        btc: float,
        secs_remaining: float,
        up_asks: List[BookLevel],
        down_asks: List[BookLevel],
    ) -> Optional[dict]:
        """Check for trading signal."""
        if not self.opening_price or secs_remaining < 30:
            return None
        
        p_up = self.calc_probability(btc, self.opening_price, secs_remaining)
        p_down = 1 - p_up
        
        # Check Up side
        up_ask = self.find_best_ask(up_asks, self.max_price)
        if up_ask:
            edge = (p_up - up_ask.price) * 100
            if edge >= self.min_edge:
                return {
                    "side": "up",
                    "price": up_ask.price,
                    "size": up_ask.size,
                    "true_prob": p_up,
                    "edge": edge,
                }
        
        # Check Down side
        down_ask = self.find_best_ask(down_asks, self.max_price)
        if down_ask:
            edge = (p_down - down_ask.price) * 100
            if edge >= self.min_edge:
                return {
                    "side": "down",
                    "price": down_ask.price,
                    "size": down_ask.size,
                    "true_prob": p_down,
                    "edge": edge,
                }
        
        return None
    
    def execute_trade(self, signal: dict, window_slug: str):
        """Execute paper trade."""
        # Check if already in position
        open_trades = [t for t in self.trades if t.status == "open"]
        if len(open_trades) >= 3:
            return
        
        trade = Trade(
            ts=time.time(),
            side=signal["side"],
            entry_price=signal["price"],
            size_usd=min(self.trade_size, signal["size"]),
            true_prob=signal["true_prob"],
            edge_cents=signal["edge"],
        )
        
        self.trades.append(trade)
        
        print(f"\n*** TRADE: Buy {signal['side'].upper()} @ {signal['price']:.4f} ***")
        print(f"    True P: {signal['true_prob']*100:.1f}%, Edge: {signal['edge']:.1f}c")
        print(f"    Available size: ${signal['size']:.0f}")
    
    def settle_trades(self, winner: str):
        """Settle all open trades."""
        for trade in self.trades:
            if trade.status == "open":
                if trade.side == winner:
                    shares = trade.size_usd / trade.entry_price
                    trade.exit_price = 1.0
                    trade.pnl = shares - trade.size_usd
                else:
                    trade.exit_price = 0.0
                    trade.pnl = -trade.size_usd
                
                trade.status = "closed"
                self.total_pnl += trade.pnl
                
                result = "WIN" if trade.pnl > 0 else "LOSS"
                print(f"\n  SETTLED: {trade.side.upper()} -> {result} (${trade.pnl:.2f})")
    
    def run(self, duration_minutes: float = 20):
        """Run the smart trader."""
        print("\n" + "=" * 70)
        print("BTC 15-MIN SMART TRADER")
        print("=" * 70)
        print(f"Strategy: Find deep book liquidity at edge prices")
        print(f"Min edge: {self.min_edge}c | Max price: {self.max_price}")
        print(f"Duration: {duration_minutes} minutes")
        print("=" * 70)
        
        start = time.time()
        deadline = start + duration_minutes * 60
        last_slug = None
        tick = 0
        
        try:
            while time.time() < deadline:
                tick += 1
                elapsed = (time.time() - start) / 60
                
                # Get window info
                info = self.get_window_info()
                secs_remaining = info["seconds_remaining"]
                
                # New window?
                if info["slug"] != last_slug:
                    # Settle previous window
                    if last_slug and self.opening_price:
                        btc = self.fetch_btc()
                        if btc > 0:
                            winner = "up" if btc >= self.opening_price else "down"
                            self.settle_trades(winner)
                    
                    print(f"\n[{elapsed:.1f}m] NEW WINDOW: {info['slug']}")
                    last_slug = info["slug"]
                    self.opening_price = None
                
                # Record opening price
                if self.opening_price is None and secs_remaining < 890:
                    self.opening_price = self.fetch_btc()
                    if self.opening_price:
                        print(f"  Opening BTC: ${self.opening_price:,.2f}")
                
                if not info["token_up"] or not info["token_down"]:
                    time.sleep(2)
                    continue
                
                # Fetch books
                up_bids, up_asks = self.fetch_book(info["token_up"])
                down_bids, down_asks = self.fetch_book(info["token_down"])
                
                # Get current BTC
                btc = self.fetch_btc()
                
                if btc > 0 and self.opening_price and secs_remaining > 30:
                    # Calculate current probability
                    p_up = self.calc_probability(btc, self.opening_price, secs_remaining)
                    distance = (btc - self.opening_price) / self.opening_price * 100
                    
                    # Find best available asks
                    up_ask = self.find_best_ask(up_asks, self.max_price)
                    down_ask = self.find_best_ask(down_asks, self.max_price)
                    
                    up_price = up_ask.price if up_ask else "N/A"
                    down_price = down_ask.price if down_ask else "N/A"
                    
                    # Check for signal
                    signal = self.check_signal(btc, secs_remaining, up_asks, down_asks)
                    if signal:
                        self.execute_trade(signal, info["slug"])
                    
                    # Status
                    if tick % 5 == 0:
                        up_edge = (p_up - (up_ask.price if up_ask else 1)) * 100 if up_ask else 0
                        down_edge = ((1-p_up) - (down_ask.price if down_ask else 1)) * 100 if down_ask else 0
                        
                        print(f"\r  [{secs_remaining:>3.0f}s] BTC: ${btc:,.0f} | Dist: {distance:+.3f}% | "
                              f"P(Up): {p_up*100:.1f}% | "
                              f"Up@{up_price} ({up_edge:+.1f}c) | "
                              f"Down@{down_price} ({down_edge:+.1f}c) | "
                              f"P&L: ${self.total_pnl:.2f}", end="")
                
                time.sleep(2)
        
        except KeyboardInterrupt:
            print("\nInterrupted")
        
        self._print_results()
    
    def _print_results(self):
        """Print results."""
        print("\n\n" + "=" * 70)
        print("SMART TRADER RESULTS")
        print("=" * 70)
        
        print(f"\nTrades: {len(self.trades)}")
        
        closed = [t for t in self.trades if t.status == "closed"]
        if closed:
            wins = [t for t in closed if t.pnl > 0]
            print(f"Wins: {len(wins)} / {len(closed)} ({len(wins)/len(closed)*100:.0f}%)")
            print(f"Total P&L: ${self.total_pnl:.2f}")
            
            for t in closed:
                result = "WIN" if t.pnl > 0 else "LOSS"
                print(f"  {t.side.upper()} @ {t.entry_price:.4f} | P={t.true_prob*100:.1f}% | "
                      f"Edge={t.edge_cents:.1f}c | {result} ${t.pnl:.2f}")
        
        if self.total_pnl > 0:
            print(f"\n*** PROFITABLE: ${self.total_pnl:.2f} ***")
        elif self.total_pnl < 0:
            print(f"\n*** LOSS: ${self.total_pnl:.2f} ***")
        else:
            print("\n*** BREAK EVEN ***")
        
        # Save
        results = {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "total_pnl": self.total_pnl,
            "trades": len(self.trades),
            "trade_details": [
                {"side": t.side, "entry": t.entry_price, "prob": t.true_prob, 
                 "edge": t.edge_cents, "pnl": t.pnl, "status": t.status}
                for t in self.trades
            ]
        }
        
        with open(self.output_dir / "smart_results.json", "w") as f:
            json.dump(results, f, indent=2)


def main():
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument("--duration", type=float, default=20)
    parser.add_argument("--edge", type=float, default=3.0)
    parser.add_argument("--max-price", type=float, default=0.90)
    args = parser.parse_args()
    
    bot = BTCSmartTrader(
        min_edge_cents=args.edge,
        max_price=args.max_price,
    )
    bot.run(duration_minutes=args.duration)


if __name__ == "__main__":
    main()

