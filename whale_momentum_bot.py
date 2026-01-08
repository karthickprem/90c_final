"""
WHALE MOMENTUM BOT - Detect and Follow Large Trades

Since we can't directly access @PolywhalesALERT, we detect whale activity by:
1. Monitoring price movements across markets
2. When price moves sharply (>2% in short time) = whale entered
3. Follow the direction of the whale

This is a proven strategy: momentum following works because:
- Large trades move markets
- Whales often have information edge
- Following momentum captures the continuation
"""

import requests
import json
import time
import statistics
from datetime import datetime, timezone
from pathlib import Path
from dataclasses import dataclass, field
from typing import List, Dict, Optional

GAMMA_API = "https://gamma-api.polymarket.com"
CLOB_API = "https://clob.polymarket.com"

session = requests.Session()


@dataclass
class PricePoint:
    """A price observation."""
    ts: float
    bid: float
    ask: float
    mid: float


@dataclass
class MarketState:
    """Tracking state for a market."""
    slug: str
    token_id: str
    outcome: str
    prices: List[PricePoint] = field(default_factory=list)
    last_signal: Optional[str] = None
    last_signal_ts: float = 0


@dataclass 
class Trade:
    """A paper trade."""
    ts: float
    market_slug: str
    outcome: str
    side: str  # "long" or "short"
    entry_price: float
    size_usd: float
    exit_price: Optional[float] = None
    pnl: Optional[float] = None
    status: str = "open"  # open, closed
    
    def close(self, exit_price: float):
        self.exit_price = exit_price
        self.status = "closed"
        if self.side == "long":
            self.pnl = (exit_price - self.entry_price) * (self.size_usd / self.entry_price)
        else:
            self.pnl = (self.entry_price - exit_price) * (self.size_usd / self.entry_price)


class WhaleMomentumBot:
    """
    Detects whale activity via price momentum and follows trades.
    """
    
    def __init__(
        self,
        # Signal detection
        momentum_threshold: float = 0.02,  # 2% price move = whale signal
        lookback_ticks: int = 10,  # Compare current to N ticks ago
        
        # Trading params
        trade_size: float = 20.0,  # USD per trade
        max_positions: int = 5,
        take_profit: float = 0.03,  # 3% profit target
        stop_loss: float = 0.02,  # 2% stop loss
        hold_time_max: float = 300,  # Max hold 5 minutes
        
        # Risk
        cooldown_seconds: float = 60,  # Wait after signal before new one
        
        output_dir: str = "whale_results",
    ):
        self.momentum_threshold = momentum_threshold
        self.lookback_ticks = lookback_ticks
        self.trade_size = trade_size
        self.max_positions = max_positions
        self.take_profit = take_profit
        self.stop_loss = stop_loss
        self.hold_time_max = hold_time_max
        self.cooldown_seconds = cooldown_seconds
        
        self.output_dir = Path(output_dir)
        self.output_dir.mkdir(exist_ok=True)
        
        # State
        self.markets: Dict[str, MarketState] = {}
        self.trades: List[Trade] = []
        self.signals_detected = 0
        self.total_pnl = 0.0
        
        # Logging
        ts_str = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
        self.log_path = self.output_dir / f"whale_momentum_{ts_str}.jsonl"
        self.log_file = None
    
    def _log(self, event: str, data: dict):
        if self.log_file:
            record = {"ts": time.time(), "event": event, **data}
            self.log_file.write(json.dumps(record) + "\n")
            self.log_file.flush()
    
    def fetch_active_markets(self, limit: int = 20) -> List[dict]:
        """Fetch active markets with good liquidity."""
        try:
            r = session.get(f"{GAMMA_API}/markets", params={
                "active": "true",
                "closed": "false",
                "limit": str(limit * 2),
            }, timeout=10)
            markets = r.json()
            
            # Filter for liquid markets
            liquid = []
            for m in markets:
                volume = float(m.get("volume", 0) or 0)
                liquidity = float(m.get("liquidity", 0) or 0)
                if volume > 10000 and liquidity > 5000:
                    liquid.append(m)
            
            return liquid[:limit]
        except Exception as e:
            return []
    
    def fetch_price(self, token_id: str) -> Optional[PricePoint]:
        """Fetch current price for a token."""
        try:
            r = session.get(f"{CLOB_API}/book", params={"token_id": token_id}, timeout=5)
            book = r.json()
            
            bids = book.get("bids", [])
            asks = book.get("asks", [])
            
            if not bids or not asks:
                return None
            
            bid = float(bids[0]["price"])
            ask = float(asks[0]["price"])
            mid = (bid + ask) / 2
            
            return PricePoint(ts=time.time(), bid=bid, ask=ask, mid=mid)
        except:
            return None
    
    def detect_momentum(self, state: MarketState) -> Optional[str]:
        """
        Detect if there's a momentum signal.
        Returns "bullish" or "bearish" or None.
        """
        if len(state.prices) < self.lookback_ticks:
            return None
        
        current = state.prices[-1]
        old = state.prices[-self.lookback_ticks]
        
        price_change = (current.mid - old.mid) / old.mid if old.mid > 0 else 0
        
        # Check cooldown
        if time.time() - state.last_signal_ts < self.cooldown_seconds:
            return None
        
        if price_change >= self.momentum_threshold:
            return "bullish"
        elif price_change <= -self.momentum_threshold:
            return "bearish"
        
        return None
    
    def open_position(self, state: MarketState, direction: str):
        """Open a new position following momentum."""
        if len([t for t in self.trades if t.status == "open"]) >= self.max_positions:
            return None
        
        # Check if already in this market
        for t in self.trades:
            if t.status == "open" and t.market_slug == state.slug:
                return None
        
        current_price = state.prices[-1].ask if direction == "bullish" else state.prices[-1].bid
        
        trade = Trade(
            ts=time.time(),
            market_slug=state.slug,
            outcome=state.outcome,
            side="long" if direction == "bullish" else "short",
            entry_price=current_price,
            size_usd=self.trade_size,
        )
        
        self.trades.append(trade)
        state.last_signal = direction
        state.last_signal_ts = time.time()
        self.signals_detected += 1
        
        self._log("OPEN_POSITION", {
            "slug": state.slug,
            "outcome": state.outcome,
            "direction": direction,
            "entry_price": current_price,
            "size": self.trade_size,
        })
        
        return trade
    
    def check_exits(self, state: MarketState):
        """Check if any open positions should be closed."""
        current_price = state.prices[-1].mid if state.prices else 0
        
        for trade in self.trades:
            if trade.status != "open" or trade.market_slug != state.slug:
                continue
            
            # Calculate current P&L
            if trade.side == "long":
                pnl_pct = (current_price - trade.entry_price) / trade.entry_price
            else:
                pnl_pct = (trade.entry_price - current_price) / trade.entry_price
            
            exit_reason = None
            
            # Take profit
            if pnl_pct >= self.take_profit:
                exit_reason = "take_profit"
            
            # Stop loss
            elif pnl_pct <= -self.stop_loss:
                exit_reason = "stop_loss"
            
            # Max hold time
            elif time.time() - trade.ts >= self.hold_time_max:
                exit_reason = "timeout"
            
            if exit_reason:
                exit_price = state.prices[-1].bid if trade.side == "long" else state.prices[-1].ask
                trade.close(exit_price)
                self.total_pnl += trade.pnl or 0
                
                self._log("CLOSE_POSITION", {
                    "slug": state.slug,
                    "outcome": state.outcome,
                    "reason": exit_reason,
                    "entry": trade.entry_price,
                    "exit": exit_price,
                    "pnl": trade.pnl,
                })
                
                pnl_display = f"+${trade.pnl:.2f}" if trade.pnl >= 0 else f"-${abs(trade.pnl):.2f}"
                print(f"  CLOSED: {state.outcome} ({exit_reason}) {pnl_display}")
    
    def run(self, duration_minutes: float = 30):
        """Run the whale momentum bot."""
        print("\n" + "=" * 70)
        print("WHALE MOMENTUM BOT")
        print("=" * 70)
        print(f"Strategy: Detect price momentum (>{self.momentum_threshold*100:.1f}%) and follow")
        print(f"Trade size: ${self.trade_size}")
        print(f"Take profit: {self.take_profit*100:.1f}% | Stop loss: {self.stop_loss*100:.1f}%")
        print(f"Duration: {duration_minutes} minutes")
        print("=" * 70)
        
        # Initialize markets
        print("\nFetching markets...")
        raw_markets = self.fetch_active_markets(limit=15)
        
        for m in raw_markets:
            slug = m.get("slug", "")
            tokens = m.get("clobTokenIds", [])
            outcomes = m.get("outcomes", [])
            
            if isinstance(tokens, str):
                tokens = json.loads(tokens)
            if isinstance(outcomes, str):
                outcomes = json.loads(outcomes)
            
            if tokens and outcomes:
                # Track first outcome (YES side typically)
                state = MarketState(
                    slug=slug,
                    token_id=tokens[0],
                    outcome=str(outcomes[0]),
                )
                self.markets[slug] = state
        
        print(f"Monitoring {len(self.markets)} markets")
        
        self.log_file = open(self.log_path, "w", encoding="utf-8")
        
        start = time.time()
        deadline = start + duration_minutes * 60
        tick = 0
        
        try:
            while time.time() < deadline:
                tick += 1
                elapsed = (time.time() - start) / 60
                
                for slug, state in self.markets.items():
                    # Fetch price
                    price = self.fetch_price(state.token_id)
                    if not price:
                        continue
                    
                    state.prices.append(price)
                    
                    # Keep last 50 prices
                    if len(state.prices) > 50:
                        state.prices = state.prices[-50:]
                    
                    # Check exits first
                    self.check_exits(state)
                    
                    # Detect momentum signal
                    signal = self.detect_momentum(state)
                    if signal:
                        trade = self.open_position(state, signal)
                        if trade:
                            print(f"\n[{elapsed:.1f}m] WHALE SIGNAL: {signal.upper()} on {state.outcome[:30]}")
                            print(f"  Entry: {trade.entry_price:.4f}, Size: ${trade.size_usd}")
                    
                    time.sleep(0.05)  # Rate limit
                
                # Status update
                open_positions = len([t for t in self.trades if t.status == "open"])
                closed_trades = len([t for t in self.trades if t.status == "closed"])
                
                if tick % 20 == 0:
                    print(f"\r[{elapsed:.1f}m] Signals: {self.signals_detected} | "
                          f"Open: {open_positions} | Closed: {closed_trades} | "
                          f"P&L: ${self.total_pnl:.2f}", end="")
                
                time.sleep(0.3)
        
        except KeyboardInterrupt:
            print("\nInterrupted")
        finally:
            self.log_file.close()
        
        self._print_results()
    
    def _print_results(self):
        """Print trading results."""
        print("\n\n" + "=" * 70)
        print("WHALE MOMENTUM RESULTS")
        print("=" * 70)
        
        closed = [t for t in self.trades if t.status == "closed"]
        open_trades = [t for t in self.trades if t.status == "open"]
        
        print(f"\nSignals detected: {self.signals_detected}")
        print(f"Trades executed: {len(self.trades)}")
        print(f"Trades closed: {len(closed)}")
        print(f"Trades still open: {len(open_trades)}")
        
        if closed:
            wins = [t for t in closed if t.pnl and t.pnl > 0]
            losses = [t for t in closed if t.pnl and t.pnl <= 0]
            
            print(f"\nWins: {len(wins)}")
            print(f"Losses: {len(losses)}")
            
            if len(closed) > 0:
                win_rate = len(wins) / len(closed) * 100
                print(f"Win rate: {win_rate:.1f}%")
            
            pnls = [t.pnl for t in closed if t.pnl is not None]
            if pnls:
                total_pnl = sum(pnls)
                avg_pnl = statistics.mean(pnls)
                print(f"\nTotal P&L: ${total_pnl:.2f}")
                print(f"Average P&L per trade: ${avg_pnl:.2f}")
                
                if wins:
                    avg_win = statistics.mean([t.pnl for t in wins])
                    print(f"Average win: ${avg_win:.2f}")
                
                if losses:
                    avg_loss = statistics.mean([t.pnl for t in losses])
                    print(f"Average loss: ${avg_loss:.2f}")
        
        # Open positions
        if open_trades:
            print(f"\nOpen positions:")
            for t in open_trades:
                print(f"  {t.outcome}: {t.side} @ {t.entry_price:.4f}")
        
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
            "signals_detected": self.signals_detected,
            "total_trades": len(self.trades),
            "closed_trades": len(closed),
            "total_pnl": self.total_pnl,
            "trades": [
                {
                    "slug": t.market_slug,
                    "outcome": t.outcome,
                    "side": t.side,
                    "entry": t.entry_price,
                    "exit": t.exit_price,
                    "pnl": t.pnl,
                    "status": t.status,
                }
                for t in self.trades
            ],
        }
        
        results_path = self.output_dir / "whale_momentum_results.json"
        with open(results_path, "w") as f:
            json.dump(results, f, indent=2)
        
        print(f"\nResults saved to: {results_path}")


def main():
    import argparse
    
    parser = argparse.ArgumentParser(description="Whale Momentum Bot")
    parser.add_argument("--duration", type=float, default=15, help="Duration in minutes")
    parser.add_argument("--threshold", type=float, default=0.02, help="Momentum threshold (0.02 = 2%)")
    parser.add_argument("--size", type=float, default=20, help="Trade size in USD")
    parser.add_argument("--take-profit", type=float, default=0.03, help="Take profit (0.03 = 3%)")
    parser.add_argument("--stop-loss", type=float, default=0.02, help="Stop loss (0.02 = 2%)")
    
    args = parser.parse_args()
    
    bot = WhaleMomentumBot(
        momentum_threshold=args.threshold,
        trade_size=args.size,
        take_profit=args.take_profit,
        stop_loss=args.stop_loss,
    )
    
    bot.run(duration_minutes=args.duration)


if __name__ == "__main__":
    main()

