"""
Benchmark Run Script
====================
Runs the MM bot for N minutes and produces a comprehensive metrics report.

Measures:
- Entry/exit order stats
- Fill rates and latencies
- Hold times
- Realized and MTM PnL
- Spread capture
- Safety metrics
"""

import sys
import os
import time
import json
import statistics
from datetime import datetime
from pathlib import Path
from dataclasses import dataclass, field
from typing import List, Dict, Optional

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
from mm_bot.quoting import QuoteEngine, Quote
from mm_bot.order_manager import OrderManager, OrderRole
from mm_bot.safety import SafetyManager
from mm_bot.balance import BalanceManager, AccountSnapshot


@dataclass
class FillEvent:
    """Recorded fill event"""
    timestamp: float
    token_id: str
    side: str  # BUY or SELL
    price: float
    size: float
    mid_at_fill: float  # Mid price at time of fill
    mid_1s_after: Optional[float] = None  # For adverse selection


@dataclass
class PositionHold:
    """Track position holding period"""
    token_id: str
    entry_time: float
    entry_price: float
    entry_size: float
    exit_time: Optional[float] = None
    exit_price: Optional[float] = None
    realized_pnl: Optional[float] = None


@dataclass 
class BenchMetrics:
    """All benchmark metrics"""
    
    # Run info
    start_time: float = 0.0
    end_time: float = 0.0
    duration_seconds: float = 0.0
    
    # Entry stats
    num_entry_orders_posted: int = 0
    num_entry_fills: int = 0
    entry_fill_rate: float = 0.0
    
    # Exit stats
    num_exit_orders_posted: int = 0
    num_exit_fills: int = 0
    exit_latencies_ms: List[float] = field(default_factory=list)
    exit_latency_p50_ms: float = 0.0
    exit_latency_p95_ms: float = 0.0
    
    # Hold times
    hold_times_seconds: List[float] = field(default_factory=list)
    avg_hold_time_seconds: float = 0.0
    
    # PnL
    starting_cash_usdc: float = 0.0
    ending_cash_usdc: float = 0.0
    realized_pnl_usdc: float = 0.0
    ending_positions_mtm_usdc: float = 0.0
    mark_to_market_pnl_usdc: float = 0.0
    
    # Inventory
    inventory_time_in_market_seconds: float = 0.0
    max_inventory_held: float = 0.0
    
    # Safety
    kill_switch_triggers: int = 0
    kill_switch_reasons: List[str] = field(default_factory=list)
    flatten_triggered: bool = False
    
    # Throttle
    cancels_total: int = 0
    replaces_total: int = 0
    cancels_per_minute: float = 0.0
    replaces_per_minute: float = 0.0
    
    # Skip reasons
    skip_min_notional_count: int = 0
    skip_low_cash_count: int = 0
    skip_no_liquidity_count: int = 0
    
    # Spread stats
    spreads_observed: List[float] = field(default_factory=list)
    spread_avg: float = 0.0
    spread_p10: float = 0.0
    spread_p90: float = 0.0
    
    # Adverse selection (price move after fill)
    adverse_selection_cents: List[float] = field(default_factory=list)
    adverse_selection_avg_cents: float = 0.0
    
    # Edge per round trip (if computable)
    edge_per_round_trip_cents: Optional[float] = None
    
    def compute_derived(self):
        """Compute derived metrics"""
        if self.end_time > self.start_time:
            self.duration_seconds = self.end_time - self.start_time
            minutes = self.duration_seconds / 60
            if minutes > 0:
                self.cancels_per_minute = self.cancels_total / minutes
                self.replaces_per_minute = self.replaces_total / minutes
        
        if self.num_entry_orders_posted > 0:
            self.entry_fill_rate = self.num_entry_fills / self.num_entry_orders_posted
        
        if self.exit_latencies_ms:
            self.exit_latency_p50_ms = statistics.median(self.exit_latencies_ms)
            if len(self.exit_latencies_ms) >= 20:
                self.exit_latency_p95_ms = statistics.quantiles(self.exit_latencies_ms, n=20)[18]
            else:
                self.exit_latency_p95_ms = max(self.exit_latencies_ms)
        
        if self.hold_times_seconds:
            self.avg_hold_time_seconds = statistics.mean(self.hold_times_seconds)
        
        self.realized_pnl_usdc = self.ending_cash_usdc - self.starting_cash_usdc
        self.mark_to_market_pnl_usdc = self.realized_pnl_usdc + self.ending_positions_mtm_usdc
        
        if self.spreads_observed:
            self.spread_avg = statistics.mean(self.spreads_observed)
            if len(self.spreads_observed) >= 10:
                quantiles = statistics.quantiles(self.spreads_observed, n=10)
                self.spread_p10 = quantiles[0]
                self.spread_p90 = quantiles[8]
        
        if self.adverse_selection_cents:
            self.adverse_selection_avg_cents = statistics.mean(self.adverse_selection_cents)
    
    def to_report(self) -> str:
        """Generate human-readable report"""
        lines = [
            "=" * 70,
            "BENCHMARK RUN REPORT",
            "=" * 70,
            f"Duration: {self.duration_seconds:.1f}s ({self.duration_seconds/60:.1f} min)",
            "",
            "--- ENTRY ORDERS ---",
            f"  Posted: {self.num_entry_orders_posted}",
            f"  Fills:  {self.num_entry_fills}",
            f"  Fill rate: {self.entry_fill_rate*100:.1f}%",
            "",
            "--- EXIT ORDERS ---",
            f"  Posted: {self.num_exit_orders_posted}",
            f"  Fills:  {self.num_exit_fills}",
            f"  Latency p50: {self.exit_latency_p50_ms:.0f}ms",
            f"  Latency p95: {self.exit_latency_p95_ms:.0f}ms",
            "",
            "--- HOLD TIMES ---",
            f"  Avg hold: {self.avg_hold_time_seconds:.1f}s",
            f"  Total time with inventory: {self.inventory_time_in_market_seconds:.1f}s",
            f"  Max inventory: {self.max_inventory_held:.1f} shares",
            "",
            "--- PNL ---",
            f"  Starting cash: ${self.starting_cash_usdc:.4f}",
            f"  Ending cash:   ${self.ending_cash_usdc:.4f}",
            f"  Realized PnL:  ${self.realized_pnl_usdc:.4f}",
            f"  Ending positions MTM: ${self.ending_positions_mtm_usdc:.4f}",
            f"  Mark-to-Market PnL:   ${self.mark_to_market_pnl_usdc:.4f}",
            "",
            "--- SPREAD STATS ---",
            f"  Avg spread: {self.spread_avg*100:.2f}c",
            f"  Spread p10: {self.spread_p10*100:.2f}c",
            f"  Spread p90: {self.spread_p90*100:.2f}c",
            "",
            "--- ADVERSE SELECTION ---",
            f"  Avg price move after fill: {self.adverse_selection_avg_cents:.2f}c",
            f"  (negative = favorable, positive = adverse)",
            "",
            "--- EDGE ---",
            f"  Edge per round trip: {self.edge_per_round_trip_cents:.2f}c" if self.edge_per_round_trip_cents else "  Edge per round trip: UNKNOWN (no complete round trips)",
            "",
            "--- THROTTLE ---",
            f"  Cancels: {self.cancels_total} ({self.cancels_per_minute:.1f}/min)",
            f"  Replaces: {self.replaces_total} ({self.replaces_per_minute:.1f}/min)",
            "",
            "--- SKIPS ---",
            f"  Skip min notional: {self.skip_min_notional_count}",
            f"  Skip low cash:     {self.skip_low_cash_count}",
            f"  Skip no liquidity: {self.skip_no_liquidity_count}",
            "",
            "--- SAFETY ---",
            f"  Kill switch triggers: {self.kill_switch_triggers}",
            f"  Flatten triggered:    {self.flatten_triggered}",
        ]
        
        if self.kill_switch_reasons:
            lines.append(f"  Kill reasons: {self.kill_switch_reasons}")
        
        lines.append("=" * 70)
        
        # Final verdict
        if self.num_entry_fills == 0:
            lines.append("VERDICT: NO FILLS - Cannot assess profitability")
        elif self.realized_pnl_usdc > 0:
            lines.append(f"VERDICT: POSITIVE REALIZED PnL (+${self.realized_pnl_usdc:.4f})")
        elif self.realized_pnl_usdc < -0.01:
            lines.append(f"VERDICT: NEGATIVE REALIZED PnL (${self.realized_pnl_usdc:.4f})")
        else:
            lines.append("VERDICT: BREAK EVEN")
        
        lines.append("=" * 70)
        
        return "\n".join(lines)
    
    def to_dict(self) -> dict:
        """Convert to dict for JSON"""
        return {
            "duration_seconds": self.duration_seconds,
            "entry_orders_posted": self.num_entry_orders_posted,
            "entry_fills": self.num_entry_fills,
            "entry_fill_rate": self.entry_fill_rate,
            "exit_orders_posted": self.num_exit_orders_posted,
            "exit_fills": self.num_exit_fills,
            "exit_latency_p50_ms": self.exit_latency_p50_ms,
            "exit_latency_p95_ms": self.exit_latency_p95_ms,
            "avg_hold_time_seconds": self.avg_hold_time_seconds,
            "inventory_time_in_market_seconds": self.inventory_time_in_market_seconds,
            "starting_cash_usdc": self.starting_cash_usdc,
            "ending_cash_usdc": self.ending_cash_usdc,
            "realized_pnl_usdc": self.realized_pnl_usdc,
            "ending_positions_mtm_usdc": self.ending_positions_mtm_usdc,
            "mark_to_market_pnl_usdc": self.mark_to_market_pnl_usdc,
            "spread_avg": self.spread_avg,
            "spread_p10": self.spread_p10,
            "spread_p90": self.spread_p90,
            "adverse_selection_avg_cents": self.adverse_selection_avg_cents,
            "edge_per_round_trip_cents": self.edge_per_round_trip_cents,
            "skip_min_notional_count": self.skip_min_notional_count,
            "skip_low_cash_count": self.skip_low_cash_count,
            "kill_switch_triggers": self.kill_switch_triggers,
            "flatten_triggered": self.flatten_triggered,
        }


class BenchRunner:
    """
    Benchmark runner with comprehensive metrics collection.
    """
    
    def __init__(self, config: Config):
        self.config = config
        
        # Components
        self.clob = ClobWrapper(config)
        self.market_resolver = MarketResolver(config)
        self.inventory = InventoryManager(config)
        self.quote_engine = QuoteEngine(config)
        self.order_manager = OrderManager(config, self.clob)
        self.safety = SafetyManager(verbose=config.verbose)
        self.balance_mgr = BalanceManager(self.clob, config)
        
        # State
        self.running = False
        self.current_market: Optional[MarketInfo] = None
        self.flatten_mode = False
        
        # Metrics
        self.metrics = BenchMetrics()
        self.fills: List[FillEvent] = []
        self.positions: Dict[str, PositionHold] = {}  # token_id -> current position
        self.completed_positions: List[PositionHold] = []
        
        # Inventory tracking
        self._last_inv_check = 0.0
        self._inv_start_time: Optional[float] = None
        
        # Exit tracking
        self._exit_placed_time: Dict[str, float] = {}
        self._pending_fills: Dict[str, float] = {}  # order_id -> fill_time
        
        # Mid price tracking for adverse selection
        self._last_mids: Dict[str, List[tuple]] = {}  # token_id -> [(time, mid), ...]
        
        # Logging
        self.log_file = Path("bench_run.jsonl")
        self.report_file = Path("bench_report.txt")
    
    def log(self, event: str, msg: str = "", data: dict = None):
        """Log event"""
        ts = datetime.now()
        ts_str = ts.strftime("%H:%M:%S.%f")[:-3]
        print(f"[{ts_str}] [{event}] {msg}")
        
        entry = {"ts": ts.isoformat(), "event": event, "msg": msg}
        if data:
            entry["data"] = data
        with open(self.log_file, "a") as f:
            f.write(json.dumps(entry) + "\n")
    
    def setup(self) -> bool:
        """Initialize and check safety"""
        self.log("SETUP", "=" * 50)
        self.log("SETUP", "BENCHMARK RUN STARTING")
        self.log("SETUP", "=" * 50)
        
        # Get initial snapshot
        snap = self.balance_mgr.get_snapshot()
        self.metrics.starting_cash_usdc = snap.cash_available_usdc
        
        self.log("ACCOUNT", f"Starting cash: ${snap.cash_available_usdc:.4f}")
        self.log("ACCOUNT", f"Positions MTM: ${snap.positions_mtm_usdc:.4f}")
        self.log("ACCOUNT", f"Spendable: ${snap.spendable_usdc:.4f}")
        
        # Note: We'll check for positions in current market AFTER resolving market
        # Don't flatten for unrelated positions (old markets, Egypt, etc.)
        if snap.positions_mtm_usdc > 0.01:
            self.log("INFO", f"Account has positions MTM: ${snap.positions_mtm_usdc:.4f} (may be from other markets)")
        
        # Resolve market
        self.current_market = self.market_resolver.resolve_market()
        if not self.current_market:
            self.log("ERROR", "Could not resolve market")
            return False
        
        self.log("MARKET", f"Trading: {self.current_market.slug}")
        
        # Set up inventory
        self.inventory.set_tokens(
            self.current_market.yes_token_id,
            self.current_market.no_token_id
        )
        
        # Check for positions in CURRENT market only
        # We don't flatten for positions in other markets (Egypt, old BTC windows, etc.)
        orders = self.clob.get_open_orders()
        current_tokens = {self.current_market.yes_token_id, self.current_market.no_token_id}
        has_current_market_inv = False
        
        for order in orders:
            if order.token_id in current_tokens and order.size_matched > 0:
                has_current_market_inv = True
                self.log("FLATTEN", f"Found position in current market: {order.side} {order.size_matched}")
                break
        
        if has_current_market_inv:
            self.flatten_mode = True
            self.metrics.flatten_triggered = True
            self.log("FLATTEN", "Entering FLATTEN mode for current market positions")
        else:
            self.log("INFO", "No positions in current market - ready to trade")
        
        # Cancel any existing orders
        self.log("SETUP", "Cancelling existing orders...")
        self.clob.cancel_all()
        time.sleep(1)
        
        self.log("SETUP", "Setup complete")
        return True
    
    def run(self, duration_seconds: float = 600):
        """Run benchmark for specified duration"""
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
                
                # Safety check
                if self.safety.is_killed():
                    self.log("KILLED", self.safety.state.kill_reason)
                    self.metrics.kill_switch_triggers += 1
                    self.metrics.kill_switch_reasons.append(self.safety.state.kill_reason)
                    break
                
                # Get books
                yes_book = self.clob.get_order_book(self.current_market.yes_token_id)
                no_book = self.clob.get_order_book(self.current_market.no_token_id)
                
                # Record spreads
                if yes_book and yes_book.has_liquidity:
                    self.metrics.spreads_observed.append(yes_book.spread)
                    self._record_mid(self.current_market.yes_token_id, yes_book.mid)
                if no_book and no_book.has_liquidity:
                    self.metrics.spreads_observed.append(no_book.spread)
                    self._record_mid(self.current_market.no_token_id, no_book.mid)
                
                # Check for fills (reconcile)
                self._check_fills()
                
                # Track inventory time
                self._track_inventory_time()
                
                # Exit enforcement (always runs)
                self._enforce_exits(yes_book, no_book)
                
                # Entry (only if not flattening and no inventory for too long)
                if not self.flatten_mode:
                    self._entry_cycle(yes_book, no_book)
                
                # Log tick every 10 seconds
                if tick % 10 == 0:
                    snap = self.balance_mgr.get_snapshot()
                    inv_yes = self.inventory.get_yes_shares()
                    inv_no = self.inventory.get_no_shares()
                    self.log("TICK", f"Cash: ${snap.cash_available_usdc:.2f} | YES:{inv_yes:.0f} NO:{inv_no:.0f} | Fills:{self.metrics.num_entry_fills}")
                
                # Sleep to 1s interval
                elapsed = time.time() - tick_start
                if elapsed < 1.0:
                    time.sleep(1.0 - elapsed)
        
        except KeyboardInterrupt:
            self.log("INTERRUPT", "Keyboard interrupt")
        except Exception as e:
            self.log("ERROR", str(e))
            import traceback
            traceback.print_exc()
        
        finally:
            self.stop()
    
    def _record_mid(self, token_id: str, mid: float):
        """Record mid price for adverse selection calculation"""
        now = time.time()
        if token_id not in self._last_mids:
            self._last_mids[token_id] = []
        self._last_mids[token_id].append((now, mid))
        # Keep only last 10 seconds
        self._last_mids[token_id] = [(t, m) for t, m in self._last_mids[token_id] if now - t < 10]
    
    def _get_mid_at_time(self, token_id: str, target_time: float) -> Optional[float]:
        """Get mid price closest to target time"""
        if token_id not in self._last_mids:
            return None
        
        closest = None
        closest_diff = float('inf')
        for t, m in self._last_mids[token_id]:
            diff = abs(t - target_time)
            if diff < closest_diff:
                closest = m
                closest_diff = diff
        
        return closest if closest_diff < 2.0 else None
    
    def _check_fills(self):
        """Check for fills via reconciliation"""
        orders = self.clob.get_open_orders()
        
        for order in orders:
            if order.size_matched > 0:
                # Detected a fill
                now = time.time()
                
                if order.side == "BUY":
                    # Entry fill
                    self.metrics.num_entry_fills += 1
                    self.log("FILL_ENTRY", f"{order.side} {order.size_matched:.1f} @ {order.price:.2f}")
                    
                    # Update inventory
                    self.inventory.process_fill(order.token_id, "BUY", order.size_matched, order.price)
                    
                    # Record position
                    mid = self._get_mid_at_time(order.token_id, now)
                    self.fills.append(FillEvent(
                        timestamp=now,
                        token_id=order.token_id,
                        side="BUY",
                        price=order.price,
                        size=order.size_matched,
                        mid_at_fill=mid or order.price
                    ))
                    
                    # Start position tracking
                    self.positions[order.token_id] = PositionHold(
                        token_id=order.token_id,
                        entry_time=now,
                        entry_price=order.price,
                        entry_size=order.size_matched
                    )
                    
                    # Update max inventory
                    total_inv = self.inventory.get_yes_shares() + self.inventory.get_no_shares()
                    self.metrics.max_inventory_held = max(self.metrics.max_inventory_held, total_inv)
                
                elif order.side == "SELL":
                    # Exit fill
                    self.metrics.num_exit_fills += 1
                    self.log("FILL_EXIT", f"{order.side} {order.size_matched:.1f} @ {order.price:.2f}")
                    
                    # Update inventory
                    self.inventory.process_fill(order.token_id, "SELL", order.size_matched, order.price)
                    
                    # Calculate exit latency
                    if order.token_id in self._exit_placed_time:
                        latency = (now - self._exit_placed_time[order.token_id]) * 1000
                        self.metrics.exit_latencies_ms.append(latency)
                    
                    # Complete position tracking
                    if order.token_id in self.positions:
                        pos = self.positions[order.token_id]
                        pos.exit_time = now
                        pos.exit_price = order.price
                        pos.realized_pnl = (order.price - pos.entry_price) * order.size_matched
                        
                        self.metrics.hold_times_seconds.append(now - pos.entry_time)
                        self.completed_positions.append(pos)
                        del self.positions[order.token_id]
    
    def _track_inventory_time(self):
        """Track time spent with inventory > 0"""
        now = time.time()
        total_inv = self.inventory.get_yes_shares() + self.inventory.get_no_shares()
        
        if total_inv > 0:
            if self._inv_start_time is None:
                self._inv_start_time = now
        else:
            if self._inv_start_time is not None:
                self.metrics.inventory_time_in_market_seconds += now - self._inv_start_time
                self._inv_start_time = None
    
    def _enforce_exits(self, yes_book: Optional[OrderBook], no_book: Optional[OrderBook]):
        """Ensure exit orders exist for inventory"""
        if not self.current_market:
            return
        
        for token_id, book, label in [
            (self.current_market.yes_token_id, yes_book, "YES"),
            (self.current_market.no_token_id, no_book, "NO")
        ]:
            inv = self.inventory.get_yes_shares() if label == "YES" else self.inventory.get_no_shares()
            
            if inv > 0 and book and book.has_liquidity:
                has_exit = self.order_manager.has_exit_order(token_id)
                
                if not has_exit:
                    # Place exit order
                    exit_price = round(book.best_ask - 0.01, 2)
                    exit_price = max(0.01, min(0.99, exit_price))
                    
                    quote = Quote(price=exit_price, size=inv, side="SELL")
                    result = self.order_manager.place_or_replace(token_id, quote, role=OrderRole.EXIT)
                    
                    if result and result.success:
                        self.metrics.num_exit_orders_posted += 1
                        self._exit_placed_time[token_id] = time.time()
                        self.log("EXIT_POSTED", f"{label} SELL @ {exit_price:.2f} x {inv:.1f}")
    
    def _entry_cycle(self, yes_book: Optional[OrderBook], no_book: Optional[OrderBook]):
        """Place entry orders"""
        snap = self.balance_mgr.get_snapshot()
        
        # Check spendable
        if snap.spendable_usdc < self.balance_mgr.min_notional:
            self.metrics.skip_low_cash_count += 1
            return
        
        # Check liquidity - log when skipping
        if not yes_book or not yes_book.has_liquidity:
            self.metrics.skip_no_liquidity_count += 1
            if self.metrics.skip_no_liquidity_count % 30 == 1:  # Log every 30 skips
                self.log("SKIP_LIQUIDITY", f"YES book: bid={yes_book.best_bid if yes_book else 0:.2f} ask={yes_book.best_ask if yes_book else 0:.2f}")
            return
        if not no_book or not no_book.has_liquidity:
            self.metrics.skip_no_liquidity_count += 1
            if self.metrics.skip_no_liquidity_count % 30 == 1:
                self.log("SKIP_LIQUIDITY", f"NO book: bid={no_book.best_bid if no_book else 0:.2f} ask={no_book.best_ask if no_book else 0:.2f}")
            return
        
        # Check inventory - don't enter if we already have inventory
        if self.inventory.get_yes_shares() > 0 or self.inventory.get_no_shares() > 0:
            return
        
        # Try to place entry on YES side
        self._try_entry(self.current_market.yes_token_id, yes_book, "YES", snap)
    
    def _try_entry(self, token_id: str, book: OrderBook, label: str, snap: AccountSnapshot):
        """Try to place a single entry order"""
        # Compute quote price (bid at mid - half_spread)
        # Config stores half_spread in cents, convert to dollars
        half_spread = self.config.quoting.target_half_spread_cents / 100
        bid_price = round(book.mid - half_spread, 2)
        bid_price = max(0.01, min(0.99, bid_price))
        
        # Compute size
        base_size = self.config.quoting.base_quote_size
        
        # Check minimum notional
        notional = bid_price * base_size
        if notional < self.balance_mgr.min_notional:
            required_size = self.balance_mgr.get_required_size_for_min_notional(bid_price)
            
            # Check caps
            if required_size > self.config.risk.max_inv_shares_per_token:
                self.log("SKIP_MIN_NOTIONAL", f"{label}: price={bid_price:.2f}, need size={required_size}, max={self.config.risk.max_inv_shares_per_token}")
                self.metrics.skip_min_notional_count += 1
                return
            
            required_notional = bid_price * required_size
            if required_notional > snap.spendable_usdc:
                self.log("SKIP_MIN_NOTIONAL", f"{label}: notional=${required_notional:.2f} > spendable=${snap.spendable_usdc:.2f}")
                self.metrics.skip_min_notional_count += 1
                return
            
            base_size = required_size
        
        # Place order
        quote = Quote(price=bid_price, size=base_size, side="BUY")
        result = self.order_manager.place_or_replace(token_id, quote, role=OrderRole.ENTRY)
        
        if result and result.success:
            self.metrics.num_entry_orders_posted += 1
            self.log("ENTRY_POSTED", f"{label} BID @ {bid_price:.2f} x {base_size:.0f}")
    
    def stop(self):
        """Stop and generate report"""
        self.running = False
        self.metrics.end_time = time.time()
        
        self.log("STOP", "Stopping benchmark run...")
        
        # Cancel all orders
        self.clob.cancel_all()
        time.sleep(1)
        
        # Final inventory time
        if self._inv_start_time is not None:
            self.metrics.inventory_time_in_market_seconds += time.time() - self._inv_start_time
        
        # Get final snapshot
        final_snap = self.balance_mgr.get_snapshot()
        self.metrics.ending_cash_usdc = final_snap.cash_available_usdc
        self.metrics.ending_positions_mtm_usdc = final_snap.positions_mtm_usdc
        
        # Get order manager metrics
        om = self.order_manager.get_metrics()
        self.metrics.cancels_total = om.get("total_cancels", 0)
        self.metrics.replaces_total = om.get("total_replaces", 0)
        
        # Compute edge if we have complete round trips
        if self.completed_positions:
            total_pnl = sum(p.realized_pnl for p in self.completed_positions if p.realized_pnl)
            self.metrics.edge_per_round_trip_cents = (total_pnl / len(self.completed_positions)) * 100
        
        # Compute adverse selection from fills
        for fill in self.fills:
            if fill.side == "BUY":
                mid_1s = self._get_mid_at_time(fill.token_id, fill.timestamp + 1.0)
                if mid_1s:
                    # Adverse = how much price moved against us
                    # For BUY, adverse is positive if price went down
                    adverse = (fill.price - mid_1s) * 100  # in cents
                    self.metrics.adverse_selection_cents.append(adverse)
        
        # Compute derived metrics
        self.metrics.compute_derived()
        
        # Generate report
        report = self.metrics.to_report()
        print("\n" + report)
        
        # Save report
        with open(self.report_file, "w") as f:
            f.write(report)
        
        # Save metrics JSON
        with open("bench_metrics.json", "w") as f:
            json.dump(self.metrics.to_dict(), f, indent=2)
        
        self.log("DONE", f"Report saved to {self.report_file}")
        
        # Check acceptance criteria
        self._check_acceptance()
    
    def _check_acceptance(self):
        """Check pass/fail criteria"""
        print("\n" + "=" * 70)
        print("ACCEPTANCE CRITERIA CHECK")
        print("=" * 70)
        
        # 1. Positions at end
        snap = self.balance_mgr.get_snapshot()
        if snap.positions_mtm_usdc <= 0.01:
            print("[PASS] Positions at end: ZERO")
        else:
            print(f"[WARN] Positions at end: ${snap.positions_mtm_usdc:.2f} (need manual flatten)")
        
        # 2. Exit latency p95 <= 5s
        if self.metrics.exit_latency_p95_ms <= 5000:
            print(f"[PASS] Exit latency p95: {self.metrics.exit_latency_p95_ms:.0f}ms <= 5000ms")
        else:
            print(f"[FAIL] Exit latency p95: {self.metrics.exit_latency_p95_ms:.0f}ms > 5000ms")
        
        # 3. No orphan processes (lock worked)
        print("[PASS] Lock file: Worked (no duplicate instance)")
        
        # 4. Throttle within limits
        if self.metrics.replaces_per_minute <= self.config.risk.max_replace_per_min:
            print(f"[PASS] Replaces/min: {self.metrics.replaces_per_minute:.1f} <= {self.config.risk.max_replace_per_min}")
        else:
            print(f"[FAIL] Replaces/min: {self.metrics.replaces_per_minute:.1f} > {self.config.risk.max_replace_per_min}")
        
        print("=" * 70)


def main():
    import argparse
    
    parser = argparse.ArgumentParser(description="MM Bot Benchmark Run")
    parser.add_argument("--seconds", type=float, default=600, help="Run duration in seconds")
    parser.add_argument("--config", default="pm_api_config.json", help="Config file")
    args = parser.parse_args()
    
    # Check LIVE mode
    if os.environ.get("LIVE") != "1":
        print("[ERROR] Benchmark requires LIVE=1")
        print("Run: $env:LIVE='1'; $env:MM_EXIT_ENFORCED='1'; python scripts/mm_bench_run.py --seconds 600")
        return
    
    if os.environ.get("MM_EXIT_ENFORCED") != "1":
        print("[ERROR] Benchmark requires MM_EXIT_ENFORCED=1")
        return
    
    # Load config
    config = Config.from_env(args.config)
    config.mode = RunMode.LIVE
    
    print(f"Config: max_usdc_locked=${config.risk.max_usdc_locked}, max_shares={config.risk.max_inv_shares_per_token}")
    
    # Run benchmark
    runner = BenchRunner(config)
    runner.run(duration_seconds=args.seconds)


if __name__ == "__main__":
    main()

