"""
True Market Making Strategy V2
==============================
Near-touch quoting + two-sided + follow-the-book.

Key differences from V1:
1. Quote at best_bid+tick (improve best bid), NOT mid-based
2. Two-sided: bid on BOTH YES and NO tokens
3. Follow the book: reprice stale orders within throttle budget
4. Regime filter: pause on trend/spike, but never pause exits
5. Reliable benchmark with try/finally
"""

import sys
import os
import time
import json
import statistics
import signal
from datetime import datetime
from pathlib import Path
from dataclasses import dataclass, field
from typing import List, Dict, Optional, Tuple

# Force unbuffered output
import builtins
_print = builtins.print
def print(*args, **kwargs):
    kwargs['flush'] = True
    _print(*args, **kwargs)

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from mm_bot.config import Config, RunMode
from mm_bot.clob import ClobWrapper, OrderBook, Side
from mm_bot.market import MarketResolver, MarketInfo
from mm_bot.inventory import InventoryManager
from mm_bot.order_manager import OrderManager, OrderRole
from mm_bot.balance import BalanceManager, AccountSnapshot


@dataclass
class MakerConfig:
    """Market maker specific config"""
    # Quoting
    entry_improve_ticks: int = 1  # How many ticks to improve best bid
    max_cross_gap_ticks: int = 1  # Min gap from crossing ask
    tick_size: float = 0.01
    quote_size: int = 5  # Min 5 for Polymarket
    
    # Throttle
    max_replaces_per_min: int = 20
    
    # Regime filter
    trend_pause_ticks: int = 3  # Pause if mid moved 3+ ticks in same direction
    trend_window_secs: float = 5.0
    min_spread_to_quote: float = 0.01  # Don't quote if spread < 1c
    
    # Risk
    max_usdc_locked: float = 1.5
    max_shares_per_token: int = 50


@dataclass
class QuoteState:
    """Track quoting state per token"""
    token_id: str
    label: str  # YES or NO
    
    # Current order
    current_order_id: Optional[str] = None
    current_price: float = 0.0
    current_size: float = 0.0
    
    # Desired quote
    desired_price: float = 0.0
    
    # Stats
    orders_placed: int = 0
    replaces: int = 0
    fills: int = 0


@dataclass
class BenchMetrics:
    """Benchmark metrics"""
    start_time: float = 0.0
    end_time: float = 0.0
    
    # Orders
    entry_bids_posted: int = 0
    exit_asks_posted: int = 0
    replaces: int = 0
    replace_throttled: int = 0
    
    # Fills
    entry_fills: int = 0
    exit_fills: int = 0
    
    # Skips
    skip_no_liquidity: int = 0
    skip_spread_too_tight: int = 0
    skip_regime_filter: int = 0
    skip_min_notional: int = 0
    
    # Book snapshots
    book_snapshots: List[dict] = field(default_factory=list)
    
    # PnL
    starting_cash: float = 0.0
    ending_cash: float = 0.0
    realized_pnl: float = 0.0


class MakerBot:
    """
    True market maker bot with near-touch quoting.
    """
    
    def __init__(self, config: Config, maker_cfg: MakerConfig):
        self.config = config
        self.maker_cfg = maker_cfg
        
        # Components
        self.clob = ClobWrapper(config)
        self.market_resolver = MarketResolver(config)
        self.inventory = InventoryManager(config)
        self.order_manager = OrderManager(config, self.clob)
        self.balance_mgr = BalanceManager(self.clob, config)
        
        # State
        self.running = False
        self.current_market: Optional[MarketInfo] = None
        self.yes_quote = QuoteState(token_id="", label="YES")
        self.no_quote = QuoteState(token_id="", label="NO")
        
        # Throttle tracking
        self._replaces_this_minute = 0
        self._minute_start = 0.0
        
        # Regime filter
        self._mid_history: List[Tuple[float, float]] = []  # (time, mid)
        self._regime_pause_until = 0.0
        
        # Metrics
        self.metrics = BenchMetrics()
        
        # Logging
        self.log_file = Path("maker_v2.jsonl")
        self.report_file = Path("maker_v2_report.txt")
        
        # Signal handling
        signal.signal(signal.SIGINT, self._signal_handler)
    
    def _signal_handler(self, signum, frame):
        """Handle shutdown"""
        self.log("SHUTDOWN", "Signal received")
        self.running = False
    
    def log(self, event: str, msg: str = "", data: dict = None):
        """Log to console and file"""
        ts = datetime.now()
        ts_str = ts.strftime("%H:%M:%S.%f")[:-3]
        print(f"[{ts_str}] [{event}] {msg}")
        
        entry = {"ts": ts.isoformat(), "event": event, "msg": msg}
        if data:
            entry["data"] = data
        with open(self.log_file, "a") as f:
            f.write(json.dumps(entry) + "\n")
    
    def setup(self) -> bool:
        """Initialize"""
        self.log("SETUP", "=" * 50)
        self.log("SETUP", "MARKET MAKER V2 - NEAR TOUCH QUOTING")
        self.log("SETUP", "=" * 50)
        
        # Get initial snapshot
        snap = self.balance_mgr.get_snapshot()
        self.metrics.starting_cash = snap.cash_available_usdc
        
        self.log("ACCOUNT", f"Cash: ${snap.cash_available_usdc:.2f} | Spendable: ${snap.spendable_usdc:.2f}")
        
        # Resolve market
        self.current_market = self.market_resolver.resolve_market()
        if not self.current_market:
            self.log("ERROR", "Could not resolve market")
            return False
        
        self.log("MARKET", f"Trading: {self.current_market.slug}")
        
        # Initialize quote states
        self.yes_quote.token_id = self.current_market.yes_token_id
        self.no_quote.token_id = self.current_market.no_token_id
        
        # Set up inventory
        self.inventory.set_tokens(
            self.current_market.yes_token_id,
            self.current_market.no_token_id
        )
        
        # Cancel existing
        self.log("SETUP", "Cancelling existing orders...")
        self.clob.cancel_all()
        time.sleep(1)
        
        self.log("SETUP", f"Config: improve_ticks={self.maker_cfg.entry_improve_ticks}, quote_size={self.maker_cfg.quote_size}")
        return True
    
    def run(self, duration_seconds: float = 900):
        """Main run loop"""
        if not self.setup():
            return
        
        self.running = True
        self.metrics.start_time = time.time()
        deadline = self.metrics.start_time + duration_seconds
        
        self.log("RUN", f"Running for {duration_seconds}s ({duration_seconds/60:.1f} min)")
        
        tick = 0
        try:
            while self.running and time.time() < deadline:
                tick += 1
                tick_start = time.time()
                
                # Get books
                yes_book = self.clob.get_order_book(self.current_market.yes_token_id)
                no_book = self.clob.get_order_book(self.current_market.no_token_id)
                
                # Record book snapshot
                if tick % 10 == 0:
                    self._record_book_snapshot(yes_book, no_book)
                
                # Check for fills
                self._check_fills()
                
                # Regime filter
                regime_ok = self._check_regime(yes_book, no_book)
                
                # EXIT ENFORCEMENT (always runs)
                self._manage_exits(yes_book, no_book)
                
                # ENTRY QUOTING (only if regime allows)
                if regime_ok:
                    self._manage_entries(yes_book, no_book)
                
                # Log tick every 5 seconds
                if tick % 5 == 0:
                    self._log_tick(yes_book, no_book)
                
                # Sleep to 1s interval
                elapsed = time.time() - tick_start
                if elapsed < 1.0:
                    time.sleep(1.0 - elapsed)
        
        except Exception as e:
            self.log("ERROR", str(e))
            import traceback
            traceback.print_exc()
        
        finally:
            # ALWAYS generate report
            self._finish()
    
    def _record_book_snapshot(self, yes_book: Optional[OrderBook], no_book: Optional[OrderBook]):
        """Record book state for analysis"""
        snap = {
            "ts": time.time(),
            "yes_bid": yes_book.best_bid if yes_book else 0,
            "yes_ask": yes_book.best_ask if yes_book else 0,
            "no_bid": no_book.best_bid if no_book else 0,
            "no_ask": no_book.best_ask if no_book else 0,
            "yes_desired_bid": self.yes_quote.desired_price,
            "no_desired_bid": self.no_quote.desired_price,
        }
        self.metrics.book_snapshots.append(snap)
    
    def _check_regime(self, yes_book: Optional[OrderBook], no_book: Optional[OrderBook]) -> bool:
        """
        Check if regime allows quoting.
        Returns True if safe to quote entries.
        """
        now = time.time()
        
        # If in pause, wait it out
        if now < self._regime_pause_until:
            return False
        
        if not yes_book or not yes_book.has_liquidity:
            self.metrics.skip_no_liquidity += 1
            return False
        
        # Check spread
        if yes_book.spread < self.maker_cfg.min_spread_to_quote:
            self.metrics.skip_spread_too_tight += 1
            if self.metrics.skip_spread_too_tight % 10 == 1:
                self.log("SKIP_SPREAD", f"Spread {yes_book.spread*100:.1f}c < min {self.maker_cfg.min_spread_to_quote*100:.1f}c")
            return False
        
        # Track mid for trend detection
        mid = yes_book.mid
        self._mid_history.append((now, mid))
        
        # Keep only recent history
        self._mid_history = [(t, m) for t, m in self._mid_history if now - t < self.maker_cfg.trend_window_secs]
        
        # Check for one-directional trend
        if len(self._mid_history) >= 3:
            mids = [m for _, m in self._mid_history]
            direction = mids[-1] - mids[0]
            ticks_moved = abs(direction) / self.maker_cfg.tick_size
            
            if ticks_moved >= self.maker_cfg.trend_pause_ticks:
                self.log("REGIME_PAUSE", f"Trend detected: {ticks_moved:.0f} ticks in {self.maker_cfg.trend_window_secs}s")
                self._regime_pause_until = now + 5.0  # Pause 5 seconds
                self.metrics.skip_regime_filter += 1
                return False
        
        return True
    
    def _manage_entries(self, yes_book: Optional[OrderBook], no_book: Optional[OrderBook]):
        """
        Manage entry bids on BOTH tokens.
        Near-touch quoting: place at best_bid + tick (improve by 1 tick).
        """
        snap = self.balance_mgr.get_snapshot()
        
        # Quote YES side
        if yes_book and yes_book.has_liquidity:
            self._update_entry_quote(self.yes_quote, yes_book, snap)
        
        # Quote NO side
        if no_book and no_book.has_liquidity:
            self._update_entry_quote(self.no_quote, no_book, snap)
    
    def _update_entry_quote(self, quote_state: QuoteState, book: OrderBook, snap: AccountSnapshot):
        """
        Update a single entry bid quote.
        Near-touch: improve best_bid by entry_improve_ticks.
        """
        # Calculate desired bid price
        # Improve best bid, but stay at least max_cross_gap_ticks from ask
        improved_bid = book.best_bid + (self.maker_cfg.entry_improve_ticks * self.maker_cfg.tick_size)
        max_bid = book.best_ask - (self.maker_cfg.max_cross_gap_ticks * self.maker_cfg.tick_size)
        
        desired_bid = min(improved_bid, max_bid)
        desired_bid = round(desired_bid, 2)
        desired_bid = max(0.01, min(0.99, desired_bid))
        
        quote_state.desired_price = desired_bid
        
        # Check if current order is stale (>= 1 tick off)
        price_diff = abs(quote_state.current_price - desired_bid)
        needs_update = price_diff >= self.maker_cfg.tick_size
        
        if not needs_update and quote_state.current_order_id:
            return  # Order is still good
        
        # Check throttle
        if not self._can_replace():
            self.metrics.replace_throttled += 1
            if self.metrics.replace_throttled % 10 == 1:
                self.log("REPLACE_THROTTLED", f"{quote_state.label}: desired={desired_bid:.2f} current={quote_state.current_price:.2f}")
            return
        
        # Check balance
        size = self.maker_cfg.quote_size
        notional = desired_bid * size
        
        if notional < 1.0:  # Min notional
            self.metrics.skip_min_notional += 1
            return
        
        if notional > snap.spendable_usdc:
            self.log("SKIP_BALANCE", f"{quote_state.label}: notional ${notional:.2f} > spendable ${snap.spendable_usdc:.2f}")
            return
        
        # Cancel existing if any
        if quote_state.current_order_id:
            self.clob.cancel_order(quote_state.current_order_id)
            quote_state.replaces += 1
            self.metrics.replaces += 1
        
        # Place new order
        result = self.clob.post_order(
            token_id=quote_state.token_id,
            side=Side.BUY,
            price=desired_bid,
            size=size,
            post_only=True
        )
        
        if result.success:
            quote_state.current_order_id = result.order_id
            quote_state.current_price = desired_bid
            quote_state.current_size = size
            quote_state.orders_placed += 1
            self.metrics.entry_bids_posted += 1
            
            self.log("ENTRY_QUOTE", f"{quote_state.label} BID @ {desired_bid:.2f} x {size} (best_bid={book.best_bid:.2f} best_ask={book.best_ask:.2f})")
            self._record_replace()
        else:
            self.log("ORDER_FAIL", f"{quote_state.label}: {result.error}")
    
    def _can_replace(self) -> bool:
        """Check if we're within replace budget"""
        now = time.time()
        
        # Reset counter each minute
        if now - self._minute_start > 60:
            self._replaces_this_minute = 0
            self._minute_start = now
        
        return self._replaces_this_minute < self.maker_cfg.max_replaces_per_min
    
    def _record_replace(self):
        """Record a replace for throttling"""
        self._replaces_this_minute += 1
    
    def _manage_exits(self, yes_book: Optional[OrderBook], no_book: Optional[OrderBook]):
        """
        Manage exit asks for inventory.
        Place near-touch sell orders to exit positions.
        """
        yes_inv = self.inventory.get_yes_shares()
        no_inv = self.inventory.get_no_shares()
        
        if yes_inv > 0 and yes_book and yes_book.has_liquidity:
            self._place_exit(self.yes_quote, yes_book, yes_inv)
        
        if no_inv > 0 and no_book and no_book.has_liquidity:
            self._place_exit(self.no_quote, no_book, no_inv)
    
    def _place_exit(self, quote_state: QuoteState, book: OrderBook, inv: float):
        """Place exit ask near touch"""
        # Improve best ask by 1 tick (undercut)
        exit_price = book.best_ask - self.maker_cfg.tick_size
        exit_price = max(0.01, min(0.99, round(exit_price, 2)))
        
        # Check if exit order exists
        exit_order = self.order_manager.get_exit_order(quote_state.token_id)
        
        if exit_order:
            # Check if needs repricing
            if abs(exit_order.price - exit_price) >= self.maker_cfg.tick_size:
                if self._can_replace():
                    self.clob.cancel_order(exit_order.order_id)
                    self._record_replace()
                else:
                    return
            else:
                return  # Exit order is good
        
        # Place exit
        from mm_bot.quoting import Quote
        quote = Quote(price=exit_price, size=min(inv, self.maker_cfg.quote_size), side="SELL")
        result = self.order_manager.place_or_replace(quote_state.token_id, quote, role=OrderRole.EXIT)
        
        if result and result.success:
            self.metrics.exit_asks_posted += 1
            self.log("EXIT_QUOTE", f"{quote_state.label} ASK @ {exit_price:.2f} x {inv:.1f}")
    
    def _check_fills(self):
        """Check for fills via API"""
        orders = self.clob.get_open_orders()
        
        for order in orders:
            if order.size_matched > 0:
                if order.side == "BUY":
                    self.log("FILL_ENTRY", f"BUY {order.size_matched:.1f} @ {order.price:.2f}")
                    self.inventory.process_fill(order.token_id, "BUY", order.size_matched, order.price)
                    self.metrics.entry_fills += 1
                    
                    # Clear quote state so we can quote again
                    if order.token_id == self.yes_quote.token_id:
                        self.yes_quote.current_order_id = None
                        self.yes_quote.fills += 1
                    elif order.token_id == self.no_quote.token_id:
                        self.no_quote.current_order_id = None
                        self.no_quote.fills += 1
                
                elif order.side == "SELL":
                    self.log("FILL_EXIT", f"SELL {order.size_matched:.1f} @ {order.price:.2f}")
                    self.inventory.process_fill(order.token_id, "SELL", order.size_matched, order.price)
                    self.metrics.exit_fills += 1
    
    def _log_tick(self, yes_book: Optional[OrderBook], no_book: Optional[OrderBook]):
        """Log tick summary"""
        yes_inv = self.inventory.get_yes_shares()
        no_inv = self.inventory.get_no_shares()
        
        yes_bid = yes_book.best_bid if yes_book else 0
        yes_ask = yes_book.best_ask if yes_book else 0
        
        self.log("TICK", 
            f"YES:{yes_bid:.2f}/{yes_ask:.2f} | "
            f"Inv:Y={yes_inv:.0f} N={no_inv:.0f} | "
            f"Fills:{self.metrics.entry_fills}/{self.metrics.exit_fills} | "
            f"Quotes:{self.metrics.entry_bids_posted}"
        )
    
    def _finish(self):
        """Finish and generate report"""
        self.log("STOP", "Stopping...")
        
        # Cancel all
        self.clob.cancel_all()
        time.sleep(1)
        
        # Final metrics
        self.metrics.end_time = time.time()
        final_snap = self.balance_mgr.get_snapshot()
        self.metrics.ending_cash = final_snap.cash_available_usdc
        self.metrics.realized_pnl = self.metrics.ending_cash - self.metrics.starting_cash
        
        # Generate report
        duration = self.metrics.end_time - self.metrics.start_time
        
        report = f"""
{'='*70}
MARKET MAKER V2 - BENCHMARK REPORT
{'='*70}

Duration: {duration:.1f}s ({duration/60:.1f} min)

--- QUOTING ---
Entry bids posted:    {self.metrics.entry_bids_posted}
Exit asks posted:     {self.metrics.exit_asks_posted}
Replaces:             {self.metrics.replaces}
Replace throttled:    {self.metrics.replace_throttled}

--- FILLS ---
Entry fills:          {self.metrics.entry_fills}
Exit fills:           {self.metrics.exit_fills}
Fill rate:            {self.metrics.entry_fills / max(1, self.metrics.entry_bids_posted) * 100:.1f}%

--- SKIPS ---
No liquidity:         {self.metrics.skip_no_liquidity}
Spread too tight:     {self.metrics.skip_spread_too_tight}
Regime filter:        {self.metrics.skip_regime_filter}
Min notional:         {self.metrics.skip_min_notional}

--- PNL ---
Starting cash:        ${self.metrics.starting_cash:.4f}
Ending cash:          ${self.metrics.ending_cash:.4f}
Realized PnL:         ${self.metrics.realized_pnl:.4f}

--- QUOTE STATES ---
YES: placed={self.yes_quote.orders_placed} fills={self.yes_quote.fills} replaces={self.yes_quote.replaces}
NO:  placed={self.no_quote.orders_placed} fills={self.no_quote.fills} replaces={self.no_quote.replaces}

{'='*70}
"""
        
        if self.metrics.entry_fills == 0:
            report += "\nVERDICT: NO FILLS - Cannot assess profitability\n"
        elif self.metrics.realized_pnl > 0:
            report += f"\nVERDICT: POSITIVE PnL (+${self.metrics.realized_pnl:.4f})\n"
        else:
            report += f"\nVERDICT: NEGATIVE PnL (${self.metrics.realized_pnl:.4f})\n"
        
        report += f"{'='*70}\n"
        
        print(report)
        
        # Save report
        with open(self.report_file, "w") as f:
            f.write(report)
        
        # Save metrics JSON
        with open("maker_v2_metrics.json", "w") as f:
            json.dump({
                "duration_seconds": duration,
                "entry_bids_posted": self.metrics.entry_bids_posted,
                "exit_asks_posted": self.metrics.exit_asks_posted,
                "entry_fills": self.metrics.entry_fills,
                "exit_fills": self.metrics.exit_fills,
                "realized_pnl": self.metrics.realized_pnl,
                "replaces": self.metrics.replaces,
                "replace_throttled": self.metrics.replace_throttled,
            }, f, indent=2)
        
        self.log("DONE", f"Report saved to {self.report_file}")


def main():
    import argparse
    
    parser = argparse.ArgumentParser(description="Market Maker V2 - Near Touch")
    parser.add_argument("--seconds", type=float, default=900, help="Run duration")
    parser.add_argument("--improve-ticks", type=int, default=1, help="Ticks to improve best bid")
    parser.add_argument("--quote-size", type=int, default=5, help="Quote size in shares")
    args = parser.parse_args()
    
    # Check env
    if os.environ.get("LIVE") != "1":
        print("[ERROR] Requires LIVE=1")
        return
    
    # Load config
    config = Config.from_env("pm_api_config.json")
    config.mode = RunMode.LIVE
    
    # Maker config
    maker_cfg = MakerConfig(
        entry_improve_ticks=args.improve_ticks,
        quote_size=args.quote_size,
        max_usdc_locked=float(os.environ.get("MM_MAX_USDC_LOCKED", "1.5")),
        max_shares_per_token=int(float(os.environ.get("MM_MAX_SHARES_PER_TOKEN", "50"))),
    )
    
    print(f"Config: improve_ticks={maker_cfg.entry_improve_ticks}, quote_size={maker_cfg.quote_size}")
    
    bot = MakerBot(config, maker_cfg)
    bot.run(duration_seconds=args.seconds)


if __name__ == "__main__":
    main()

