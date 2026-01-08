"""
BTC 15-MIN PROBABILITY TRADING BOT

Uses Black-Scholes binary option pricing to calculate TRUE probability
and trades when market price differs from true probability.

ALGORITHM:
1. At window start, record opening BTC price (price to beat)
2. Every tick, fetch current BTC price
3. Calculate P(Up) using: distance from strike + time remaining + volatility
4. If market price differs from true probability by > threshold, TRADE

FORMULA:
P(Up) = N(d2) where d2 = ln(current_price / opening_price) / (vol * sqrt(T))
"""

import requests
import json
import time
import math
import statistics
from datetime import datetime, timezone
from pathlib import Path
from dataclasses import dataclass, field
from typing import List, Optional

# Try to import scipy, fall back to approximation if not available
try:
    from scipy import stats
    def norm_cdf(x):
        return stats.norm.cdf(x)
except ImportError:
    # Approximation of normal CDF
    def norm_cdf(x):
        # Abramowitz and Stegun approximation
        a1 =  0.254829592
        a2 = -0.284496736
        a3 =  1.421413741
        a4 = -1.453152027
        a5 =  1.061405429
        p  =  0.3275911
        
        sign = 1 if x >= 0 else -1
        x = abs(x)
        t = 1.0 / (1.0 + p * x)
        y = 1.0 - (((((a5*t + a4)*t) + a3)*t + a2)*t + a1)*t * math.exp(-x*x/2)
        return 0.5 * (1.0 + sign * y)

GAMMA_API = "https://gamma-api.polymarket.com"
CLOB_API = "https://clob.polymarket.com"

session = requests.Session()


@dataclass
class WindowState:
    """State for a 15-minute window."""
    slug: str
    start_ts: int
    end_ts: int
    opening_btc_price: Optional[float] = None
    token_up: str = ""
    token_down: str = ""


@dataclass
class Trade:
    """A trade record."""
    ts: float
    window_slug: str
    side: str  # "up" or "down"
    entry_price: float
    size_usd: float
    true_prob: float
    edge_cents: float
    exit_price: Optional[float] = None
    pnl: Optional[float] = None
    status: str = "open"
    settlement: Optional[str] = None  # "up" or "down"


class BTCProbabilityBot:
    """
    Trades BTC 15-min Up/Down based on probability mispricing.
    """
    
    def __init__(
        self,
        # Model params
        annual_volatility: float = 0.25,  # 25% annual vol (typical BTC)
        
        # Trading params
        min_edge_cents: float = 2.0,  # Min 2 cents edge to trade
        trade_size: float = 20.0,
        max_positions: int = 3,
        
        # Risk
        min_seconds_remaining: float = 60,  # Don't trade last 60 seconds
        max_price: float = 0.95,  # Don't pay more than 95c
        
        output_dir: str = "btc_prob_results",
    ):
        self.annual_volatility = annual_volatility
        self.min_edge_cents = min_edge_cents
        self.trade_size = trade_size
        self.max_positions = max_positions
        self.min_seconds_remaining = min_seconds_remaining
        self.max_price = max_price
        
        self.output_dir = Path(output_dir)
        self.output_dir.mkdir(exist_ok=True)
        
        # State
        self.current_window: Optional[WindowState] = None
        self.trades: List[Trade] = []
        self.btc_prices: List[tuple] = []  # (ts, price)
        
        # Stats
        self.signals_found = 0
        self.total_pnl = 0.0
        
        # Logging
        ts_str = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
        self.log_path = self.output_dir / f"btc_prob_{ts_str}.jsonl"
        self.log_file = None
    
    def _log(self, event: str, data: dict):
        if self.log_file:
            record = {"ts": time.time(), "event": event, **data}
            self.log_file.write(json.dumps(record) + "\n")
            self.log_file.flush()
    
    def fetch_btc_price(self) -> float:
        """Fetch current BTC price from CoinGecko."""
        try:
            r = session.get(
                "https://api.coingecko.com/api/v3/simple/price",
                params={"ids": "bitcoin", "vs_currencies": "usd"},
                timeout=10
            )
            data = r.json()
            return data.get("bitcoin", {}).get("usd", 0)
        except:
            return 0
    
    def get_current_window(self) -> WindowState:
        """Get or create current window state."""
        ts = int(time.time())
        window_start = ts - (ts % 900)
        window_end = window_start + 900
        slug = f"btc-updown-15m-{window_start}"
        
        # Check if we need new window
        if self.current_window is None or self.current_window.slug != slug:
            self.current_window = WindowState(
                slug=slug,
                start_ts=window_start,
                end_ts=window_end,
            )
            
            # Fetch token IDs
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
                    
                    for i, (outcome, token) in enumerate(zip(outcomes, tokens)):
                        if str(outcome).lower() == "up":
                            self.current_window.token_up = token
                        elif str(outcome).lower() == "down":
                            self.current_window.token_down = token
            except:
                pass
        
        return self.current_window
    
    def record_opening_price(self, window: WindowState):
        """
        Record the opening BTC price at window start.
        This is the "price to beat".
        """
        if window.opening_btc_price is not None:
            return  # Already recorded
        
        # Only record if we're within first 30 seconds of window
        now = time.time()
        if now - window.start_ts > 30:
            # Window already started, estimate opening from first recorded price
            if self.btc_prices:
                # Use first price we saw in this window
                for ts, price in self.btc_prices:
                    if ts >= window.start_ts:
                        window.opening_btc_price = price
                        break
            
            if window.opening_btc_price is None:
                # Fallback: use current price
                window.opening_btc_price = self.fetch_btc_price()
        else:
            # We're at window start - record fresh price
            window.opening_btc_price = self.fetch_btc_price()
        
        if window.opening_btc_price:
            print(f"\n  Opening price recorded: ${window.opening_btc_price:,.2f}")
            self._log("OPENING_PRICE", {
                "slug": window.slug,
                "opening_price": window.opening_btc_price,
            })
    
    def calculate_probability(
        self,
        current_price: float,
        opening_price: float,
        seconds_remaining: float,
    ) -> float:
        """
        Calculate P(Up) using binary option pricing.
        
        P(Up) = N(d2)
        d2 = ln(S/K) / (sigma * sqrt(T))
        """
        if opening_price <= 0 or current_price <= 0:
            return 0.5
        
        if seconds_remaining <= 0:
            return 1.0 if current_price >= opening_price else 0.0
        
        # Convert to years
        T = seconds_remaining / (365.25 * 24 * 3600)
        
        # Calculate d2
        log_ratio = math.log(current_price / opening_price)
        vol_sqrt_t = self.annual_volatility * math.sqrt(T)
        
        if vol_sqrt_t < 0.0001:
            return 1.0 if current_price >= opening_price else 0.0
        
        d2 = log_ratio / vol_sqrt_t
        
        # Probability
        prob_up = norm_cdf(d2)
        
        return prob_up
    
    def fetch_market_prices(self, window: WindowState) -> dict:
        """Fetch current market bid/ask for Up and Down."""
        result = {"up_bid": 0, "up_ask": 1, "down_bid": 0, "down_ask": 1}
        
        for side, token in [("up", window.token_up), ("down", window.token_down)]:
            if not token:
                continue
            try:
                r = session.get(f"{CLOB_API}/book", params={"token_id": token}, timeout=5)
                book = r.json()
                bids = book.get("bids", [])
                asks = book.get("asks", [])
                
                if bids:
                    result[f"{side}_bid"] = float(bids[0]["price"])
                if asks:
                    result[f"{side}_ask"] = float(asks[0]["price"])
            except:
                pass
        
        return result
    
    def check_signal(
        self,
        window: WindowState,
        current_btc: float,
        market_prices: dict,
        seconds_remaining: float,
    ) -> Optional[dict]:
        """
        Check if there's a trading signal.
        Returns signal dict or None.
        """
        if not window.opening_btc_price:
            return None
        
        if seconds_remaining < self.min_seconds_remaining:
            return None
        
        # Calculate true probability
        p_up = self.calculate_probability(current_btc, window.opening_btc_price, seconds_remaining)
        p_down = 1 - p_up
        
        # Get market prices
        up_ask = market_prices["up_ask"]
        down_ask = market_prices["down_ask"]
        
        # Check for Up edge
        if up_ask <= self.max_price:
            edge_up = (p_up - up_ask) * 100  # in cents
            if edge_up >= self.min_edge_cents:
                return {
                    "side": "up",
                    "price": up_ask,
                    "true_prob": p_up,
                    "edge_cents": edge_up,
                }
        
        # Check for Down edge
        if down_ask <= self.max_price:
            edge_down = (p_down - down_ask) * 100
            if edge_down >= self.min_edge_cents:
                return {
                    "side": "down",
                    "price": down_ask,
                    "true_prob": p_down,
                    "edge_cents": edge_down,
                }
        
        return None
    
    def execute_trade(self, window: WindowState, signal: dict):
        """Execute a trade based on signal."""
        # Check position limits
        open_trades = [t for t in self.trades if t.status == "open"]
        if len(open_trades) >= self.max_positions:
            return None
        
        # Check if already in this window
        for t in open_trades:
            if t.window_slug == window.slug:
                return None
        
        trade = Trade(
            ts=time.time(),
            window_slug=window.slug,
            side=signal["side"],
            entry_price=signal["price"],
            size_usd=self.trade_size,
            true_prob=signal["true_prob"],
            edge_cents=signal["edge_cents"],
        )
        
        self.trades.append(trade)
        self.signals_found += 1
        
        print(f"\n  *** TRADE: Buy {signal['side'].upper()} @ {signal['price']:.4f} ***")
        print(f"      True prob: {signal['true_prob']*100:.1f}%, Edge: {signal['edge_cents']:.1f}c")
        
        self._log("TRADE", {
            "slug": window.slug,
            "side": signal["side"],
            "price": signal["price"],
            "true_prob": signal["true_prob"],
            "edge": signal["edge_cents"],
        })
        
        return trade
    
    def settle_trades(self, window: WindowState, final_btc: float):
        """Settle trades for a completed window."""
        if not window.opening_btc_price:
            return
        
        # Determine winner
        winner = "up" if final_btc >= window.opening_btc_price else "down"
        
        for trade in self.trades:
            if trade.status == "open" and trade.window_slug == window.slug:
                trade.status = "settled"
                trade.settlement = winner
                
                if trade.side == winner:
                    # Won: payout is $1 per share
                    shares = trade.size_usd / trade.entry_price
                    trade.exit_price = 1.0
                    trade.pnl = shares - trade.size_usd  # Profit = payout - cost
                else:
                    # Lost: payout is $0
                    trade.exit_price = 0.0
                    trade.pnl = -trade.size_usd
                
                self.total_pnl += trade.pnl
                
                result = "WIN" if trade.pnl > 0 else "LOSS"
                print(f"\n  SETTLED: {trade.side.upper()} -> {result} (${trade.pnl:.2f})")
                
                self._log("SETTLEMENT", {
                    "slug": window.slug,
                    "side": trade.side,
                    "winner": winner,
                    "pnl": trade.pnl,
                })
    
    def run(self, duration_minutes: float = 30):
        """Run the probability trading bot."""
        print("\n" + "=" * 70)
        print("BTC 15-MIN PROBABILITY BOT")
        print("=" * 70)
        print(f"Strategy: Trade when market price differs from true probability")
        print(f"Min edge: {self.min_edge_cents}c")
        print(f"Volatility assumption: {self.annual_volatility*100:.1f}% annual")
        print(f"Duration: {duration_minutes} minutes")
        print("=" * 70)
        
        self.log_file = open(self.log_path, "w", encoding="utf-8")
        
        start = time.time()
        deadline = start + duration_minutes * 60
        tick = 0
        last_window_slug = None
        
        try:
            while time.time() < deadline:
                tick += 1
                elapsed = (time.time() - start) / 60
                now = time.time()
                
                # Get current window
                window = self.get_current_window()
                seconds_remaining = window.end_ts - now
                
                # New window?
                if window.slug != last_window_slug:
                    print(f"\n[{elapsed:.1f}m] NEW WINDOW: {window.slug}")
                    last_window_slug = window.slug
                
                # Fetch BTC price
                btc_price = self.fetch_btc_price()
                if btc_price > 0:
                    self.btc_prices.append((now, btc_price))
                    # Keep last 100
                    self.btc_prices = self.btc_prices[-100:]
                
                # Record opening price if at window start
                self.record_opening_price(window)
                
                # Settle if window ended
                if seconds_remaining <= 0:
                    if btc_price > 0:
                        self.settle_trades(window, btc_price)
                    time.sleep(2)
                    continue
                
                # Fetch market prices
                market_prices = self.fetch_market_prices(window)
                
                # Check for signal
                if window.opening_btc_price and btc_price > 0:
                    signal = self.check_signal(window, btc_price, market_prices, seconds_remaining)
                    if signal:
                        self.execute_trade(window, signal)
                    
                    # Calculate current probability for display
                    p_up = self.calculate_probability(btc_price, window.opening_btc_price, seconds_remaining)
                    
                    if tick % 10 == 0:
                        distance = (btc_price - window.opening_btc_price) / window.opening_btc_price * 100
                        print(f"\r  [{seconds_remaining:.0f}s] BTC: ${btc_price:,.0f} | "
                              f"Distance: {distance:+.3f}% | P(Up): {p_up*100:.1f}% | "
                              f"Signals: {self.signals_found} | P&L: ${self.total_pnl:.2f}", end="")
                
                time.sleep(2)  # Check every 2 seconds
        
        except KeyboardInterrupt:
            print("\nInterrupted")
        finally:
            self.log_file.close()
        
        self._print_results()
    
    def _print_results(self):
        """Print trading results."""
        print("\n\n" + "=" * 70)
        print("PROBABILITY BOT RESULTS")
        print("=" * 70)
        
        print(f"\nSignals found: {self.signals_found}")
        print(f"Trades executed: {len(self.trades)}")
        
        settled = [t for t in self.trades if t.status == "settled"]
        open_trades = [t for t in self.trades if t.status == "open"]
        
        print(f"Settled trades: {len(settled)}")
        print(f"Open trades: {len(open_trades)}")
        
        if settled:
            wins = [t for t in settled if t.pnl and t.pnl > 0]
            losses = [t for t in settled if t.pnl and t.pnl <= 0]
            
            print(f"\nWins: {len(wins)}")
            print(f"Losses: {len(losses)}")
            
            if len(settled) > 0:
                win_rate = len(wins) / len(settled) * 100
                print(f"Win rate: {win_rate:.1f}%")
            
            print(f"\nTotal P&L: ${self.total_pnl:.2f}")
            
            if wins:
                avg_win = statistics.mean([t.pnl for t in wins])
                print(f"Average win: ${avg_win:.2f}")
            
            if losses:
                avg_loss = statistics.mean([t.pnl for t in losses])
                print(f"Average loss: ${avg_loss:.2f}")
            
            print("\nTrade details:")
            for t in settled:
                result = "WIN" if t.pnl > 0 else "LOSS"
                print(f"  {t.side.upper()} @ {t.entry_price:.4f} | "
                      f"P={t.true_prob*100:.1f}% Edge={t.edge_cents:.1f}c | "
                      f"{result} ${t.pnl:.2f}")
        
        print(f"\n{'='*70}")
        if self.total_pnl > 0:
            print(f"*** PROFITABLE: ${self.total_pnl:.2f} ***")
        elif self.total_pnl < 0:
            print(f"*** LOSS: ${self.total_pnl:.2f} ***")
        else:
            print("*** BREAK EVEN ***")
        
        # Save results
        results = {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "params": {
                "volatility": self.annual_volatility,
                "min_edge": self.min_edge_cents,
            },
            "signals_found": self.signals_found,
            "total_trades": len(self.trades),
            "total_pnl": self.total_pnl,
            "trades": [
                {
                    "slug": t.window_slug,
                    "side": t.side,
                    "entry": t.entry_price,
                    "true_prob": t.true_prob,
                    "edge": t.edge_cents,
                    "settlement": t.settlement,
                    "pnl": t.pnl,
                    "status": t.status,
                }
                for t in self.trades
            ],
        }
        
        results_path = self.output_dir / "btc_prob_results.json"
        with open(results_path, "w") as f:
            json.dump(results, f, indent=2)
        
        print(f"\nResults saved to: {results_path}")


def main():
    import argparse
    
    parser = argparse.ArgumentParser(description="BTC Probability Bot")
    parser.add_argument("--duration", type=float, default=20, help="Duration in minutes")
    parser.add_argument("--edge", type=float, default=2.0, help="Min edge in cents")
    parser.add_argument("--vol", type=float, default=0.25, help="Annual volatility (0.25 = 25%)")
    parser.add_argument("--size", type=float, default=20, help="Trade size in USD")
    
    args = parser.parse_args()
    
    bot = BTCProbabilityBot(
        annual_volatility=args.vol,
        min_edge_cents=args.edge,
        trade_size=args.size,
    )
    
    bot.run(duration_minutes=args.duration)


if __name__ == "__main__":
    main()

