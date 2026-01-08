"""
Paper Execution Engine - Phase 3, 4, 5

Paper trading with two fill modes:
- Aggressive (taker): Fill at current ask immediately
- Passive (maker): Fill only if price crosses through our limit

Two strategies:
- Variant A: Instant arb (ask_up + ask_down < 1)
- Variant B: Reddit legging / DCA pair-cost (average cost < 1)
"""

import logging
import time
import json
from pathlib import Path
from datetime import datetime, timezone
from typing import Optional, List, Dict, Tuple
from dataclasses import dataclass, field
from enum import Enum

from .market_v2 import (
    MarketFetcher, 
    Window15Min, 
    OrderBookTick,
    get_current_window_slug,
)

logger = logging.getLogger(__name__)


class Side(Enum):
    UP = "up"
    DOWN = "down"


class FillMode(Enum):
    AGGRESSIVE = "aggressive"  # Taker: fill at ask
    PASSIVE = "passive"        # Maker: fill when price crosses


@dataclass
class PaperOrder:
    """A paper order."""
    order_id: str
    side: Side
    shares: float
    limit_price: float
    fill_mode: FillMode
    
    created_ts: float = 0.0
    filled_ts: float = 0.0
    fill_price: float = 0.0
    filled: bool = False
    
    def to_dict(self) -> dict:
        return {
            "order_id": self.order_id,
            "side": self.side.value,
            "shares": self.shares,
            "limit_price": self.limit_price,
            "fill_mode": self.fill_mode.value,
            "created_ts": self.created_ts,
            "filled_ts": self.filled_ts,
            "fill_price": self.fill_price,
            "filled": self.filled,
        }


@dataclass
class Position:
    """
    Position state for a window.
    
    Tracks quantities and costs for both sides.
    """
    # Quantities
    q_up: float = 0.0
    q_down: float = 0.0
    
    # Total costs paid
    cost_up: float = 0.0
    cost_down: float = 0.0
    
    # Order tracking
    orders: List[PaperOrder] = field(default_factory=list)
    fills: List[dict] = field(default_factory=list)
    
    @property
    def avg_up(self) -> float:
        """Average cost per Up share."""
        return self.cost_up / self.q_up if self.q_up > 0 else 0
    
    @property
    def avg_down(self) -> float:
        """Average cost per Down share."""
        return self.cost_down / self.q_down if self.q_down > 0 else 0
    
    @property
    def pair_cost(self) -> float:
        """Average cost of a full pair (only valid if both > 0)."""
        if self.q_up > 0 and self.q_down > 0:
            return self.avg_up + self.avg_down
        return 0
    
    @property
    def total_cost(self) -> float:
        """Total money spent."""
        return self.cost_up + self.cost_down
    
    @property
    def min_qty(self) -> float:
        """Minimum of the two sides (guaranteed payout)."""
        return min(self.q_up, self.q_down)
    
    @property
    def guaranteed_profit(self) -> float:
        """
        Guaranteed profit at settlement (worst case).
        
        gp = min(q_up, q_down) - total_cost
        
        The payout is $1 per winning share. In the worst case,
        the smaller side wins and we get min(q_up, q_down) * $1.
        """
        return self.min_qty - self.total_cost
    
    @property
    def is_hedged(self) -> bool:
        """True if we have positions on both sides."""
        return self.q_up > 0 and self.q_down > 0
    
    @property
    def imbalance(self) -> float:
        """Absolute difference between sides."""
        return abs(self.q_up - self.q_down)
    
    def record_fill(self, side: Side, shares: float, price: float, ts: float):
        """Record a fill."""
        if side == Side.UP:
            self.q_up += shares
            self.cost_up += shares * price
        else:
            self.q_down += shares
            self.cost_down += shares * price
        
        self.fills.append({
            "ts": ts,
            "side": side.value,
            "shares": shares,
            "price": price,
            "cost": shares * price,
        })
    
    def to_dict(self) -> dict:
        return {
            "q_up": self.q_up,
            "q_down": self.q_down,
            "cost_up": self.cost_up,
            "cost_down": self.cost_down,
            "avg_up": self.avg_up,
            "avg_down": self.avg_down,
            "pair_cost": self.pair_cost,
            "total_cost": self.total_cost,
            "guaranteed_profit": self.guaranteed_profit,
            "min_qty": self.min_qty,
            "imbalance": self.imbalance,
            "num_fills": len(self.fills),
        }


@dataclass
class WindowResult:
    """Result of trading a single window."""
    slug: str
    start_ts: int
    end_ts: int
    
    # Final position
    final_position: Position = None
    
    # Outcome (set after settlement)
    winning_side: Optional[Side] = None
    payout: float = 0.0
    realized_pnl: float = 0.0
    
    # Stats
    ticks_seen: int = 0
    signals_a: int = 0  # Variant A signals
    trades_made: int = 0
    
    # For analysis
    max_gp: float = 0.0  # Maximum guaranteed profit seen
    min_gp: float = 999.0  # Minimum (worst drawdown)
    
    def to_dict(self) -> dict:
        return {
            "slug": self.slug,
            "start_ts": self.start_ts,
            "end_ts": self.end_ts,
            "ticks_seen": self.ticks_seen,
            "signals_a": self.signals_a,
            "trades_made": self.trades_made,
            "final_position": self.final_position.to_dict() if self.final_position else None,
            "winning_side": self.winning_side.value if self.winning_side else None,
            "payout": self.payout,
            "realized_pnl": self.realized_pnl,
            "max_gp": self.max_gp,
            "min_gp": self.min_gp if self.min_gp < 999 else 0,
        }


class PaperEngine:
    """
    Paper trading engine.
    
    Implements both strategy variants:
    - Variant A: Instant arb scanner
    - Variant B: Reddit legging / DCA pair-cost
    """
    
    def __init__(
        self,
        # Strategy params
        instant_arb_buffer: float = 0.01,  # Variant A: buy if ask_sum < 1 - buffer
        cheap_cap: float = 0.45,           # Variant B: buy if price <= cheap_cap
        clip_size: float = 10.0,           # Shares per trade
        profit_target: float = 0.10,       # Stop when gp >= target
        stop_buffer_seconds: float = 60,   # Stop trading N seconds before end
        loss_cap: float = 5.0,             # Max acceptable loss
        
        # Fill mode
        fill_mode: FillMode = FillMode.AGGRESSIVE,
        
        # Output
        output_dir: str = "pm_results",
    ):
        self.instant_arb_buffer = instant_arb_buffer
        self.cheap_cap = cheap_cap
        self.clip_size = clip_size
        self.profit_target = profit_target
        self.stop_buffer_seconds = stop_buffer_seconds
        self.loss_cap = loss_cap
        self.fill_mode = fill_mode
        
        self.output_dir = Path(output_dir)
        self.output_dir.mkdir(exist_ok=True)
        
        self.fetcher = MarketFetcher()
        
        # Current state
        self.current_window: Optional[Window15Min] = None
        self.position: Optional[Position] = None
        self.result: Optional[WindowResult] = None
        
        # All results
        self.results: List[WindowResult] = []
        
        # Logging
        self.log_file = None
        self._open_log()
    
    def _open_log(self):
        ts = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
        self.log_path = self.output_dir / f"trades_{ts}.jsonl"
        self.log_file = open(self.log_path, "a")
    
    def _log_event(self, event_type: str, data: dict):
        event = {
            "ts": time.time(),
            "ts_iso": datetime.now(timezone.utc).isoformat(),
            "event": event_type,
            **data,
        }
        if self.log_file:
            self.log_file.write(json.dumps(event) + "\n")
            self.log_file.flush()
    
    def _switch_window(self, new_slug: str):
        """Handle window transition."""
        # Finalize previous window
        if self.result and self.position:
            self.result.final_position = self.position
            self.results.append(self.result)
            
            self._log_event("WINDOW_END", {
                "slug": self.result.slug,
                "position": self.position.to_dict(),
            })
        
        # Start new window
        self.current_window = self.fetcher.fetch_market_by_slug(new_slug)
        
        if self.current_window:
            self.position = Position()
            self.result = WindowResult(
                slug=self.current_window.slug,
                start_ts=self.current_window.start_ts,
                end_ts=self.current_window.end_ts,
            )
            
            self._log_event("WINDOW_START", {
                "slug": new_slug,
                "start_ts": self.current_window.start_ts,
                "end_ts": self.current_window.end_ts,
            })
            
            logger.info(f"Started window: {new_slug}")
    
    def _check_variant_a(self, tick: OrderBookTick) -> bool:
        """
        Variant A: Instant arb check.
        
        Signal if ask_up + ask_down < 1 - buffer
        """
        if tick.ask_sum < (1.0 - self.instant_arb_buffer):
            return True
        return False
    
    def _execute_variant_a(self, tick: OrderBookTick):
        """
        Execute Variant A: Buy both sides.
        """
        edge = 1.0 - tick.ask_sum
        shares = min(self.clip_size, tick.size_ask_up, tick.size_ask_down)
        
        if shares < 1:
            return  # Not enough depth
        
        # Buy Up
        self.position.record_fill(Side.UP, shares, tick.ask_up, tick.ts)
        
        # Buy Down
        self.position.record_fill(Side.DOWN, shares, tick.ask_down, tick.ts)
        
        self.result.trades_made += 2
        
        self._log_event("TRADE_A", {
            "ask_up": tick.ask_up,
            "ask_down": tick.ask_down,
            "ask_sum": tick.ask_sum,
            "edge": edge,
            "shares": shares,
            "position": self.position.to_dict(),
        })
        
        logger.info(f"VARIANT A: Bought {shares} shares each @ sum={tick.ask_sum:.4f}, edge={edge:.4f}")
    
    def _choose_cheap_side(self, tick: OrderBookTick) -> Tuple[Optional[Side], float, float]:
        """
        Variant B: Choose the cheaper side to buy.
        
        Returns (side, price, available_size) or (None, 0, 0)
        """
        # Determine which side is cheaper
        if tick.ask_up <= tick.ask_down:
            return Side.UP, tick.ask_up, tick.size_ask_up
        else:
            return Side.DOWN, tick.ask_down, tick.size_ask_down
    
    def _should_buy_variant_b(self, tick: OrderBookTick) -> Tuple[Optional[Side], float]:
        """
        Variant B decision logic.
        
        Returns (side_to_buy, price) or (None, 0)
        """
        cheap_side, price, size = self._choose_cheap_side(tick)
        
        if price > self.cheap_cap:
            return None, 0  # Price too high
        
        if size < 1:
            return None, 0  # No depth
        
        # Check if buying improves position
        if self.position.is_hedged:
            # If hedged and gp >= target, stop
            if self.position.guaranteed_profit >= self.profit_target:
                return None, 0
        
        # Prefer to balance the position
        if self.position.q_up > 0 and self.position.q_down == 0:
            # We have Up, need Down
            if cheap_side == Side.DOWN or tick.ask_down <= self.cheap_cap:
                return Side.DOWN, tick.ask_down
        elif self.position.q_down > 0 and self.position.q_up == 0:
            # We have Down, need Up
            if cheap_side == Side.UP or tick.ask_up <= self.cheap_cap:
                return Side.UP, tick.ask_up
        
        # Otherwise buy the cheap side
        return cheap_side, price
    
    def _execute_variant_b(self, side: Side, price: float, tick: OrderBookTick):
        """
        Execute Variant B: Buy one side.
        """
        size = tick.size_ask_up if side == Side.UP else tick.size_ask_down
        shares = min(self.clip_size, size)
        
        if shares < 1:
            return
        
        self.position.record_fill(side, shares, price, tick.ts)
        self.result.trades_made += 1
        
        self._log_event("TRADE_B", {
            "side": side.value,
            "price": price,
            "shares": shares,
            "position": self.position.to_dict(),
        })
        
        logger.info(f"VARIANT B: Bought {shares} {side.value} @ {price:.4f}, "
                   f"gp={self.position.guaranteed_profit:.4f}")
    
    def process_tick(self, tick: OrderBookTick):
        """Process a single tick."""
        if not self.result or not self.position:
            return
        
        self.result.ticks_seen += 1
        
        # Update GP tracking
        if self.position.is_hedged:
            gp = self.position.guaranteed_profit
            self.result.max_gp = max(self.result.max_gp, gp)
            self.result.min_gp = min(self.result.min_gp, gp)
        
        # Check if we should stop trading
        if tick.seconds_remaining <= self.stop_buffer_seconds:
            return  # Too close to end
        
        # Check gp target
        if self.position.is_hedged and self.position.guaranteed_profit >= self.profit_target:
            return  # Target reached
        
        # Check loss cap
        if self.position.guaranteed_profit < -self.loss_cap:
            return  # Loss cap hit
        
        # Variant A: Instant arb
        if self._check_variant_a(tick):
            self.result.signals_a += 1
            self._execute_variant_a(tick)
            return  # Variant A takes priority
        
        # Variant B: Legging / DCA
        side, price = self._should_buy_variant_b(tick)
        if side:
            self._execute_variant_b(side, price, tick)
    
    def run_window(self, window: Window15Min, poll_interval_ms: int = 500):
        """Run paper trading for a single window."""
        self._switch_window(window.slug)
        
        if not self.current_window:
            return None
        
        poll_sec = poll_interval_ms / 1000.0
        
        while True:
            now = time.time()
            
            # Check if window ended
            if now >= window.end_ts:
                break
            
            # Fetch and process tick
            tick = self.fetcher.fetch_tick(window)
            if tick and tick.ask_up > 0:
                self.process_tick(tick)
            
            time.sleep(poll_sec)
        
        # Finalize
        if self.result:
            self.result.final_position = self.position
        
        return self.result
    
    def run_continuous(self, duration_minutes: int = 60, windows_target: int = 0):
        """
        Run paper trading continuously.
        """
        print("\n" + "="*70)
        print("BTC 15-min Paper Trading Engine")
        print("="*70)
        print(f"Variant A buffer: {self.instant_arb_buffer}")
        print(f"Variant B cheap_cap: {self.cheap_cap}")
        print(f"Clip size: {self.clip_size}")
        print(f"Profit target: ${self.profit_target}")
        print(f"Stop buffer: {self.stop_buffer_seconds}s before end")
        print(f"Output: {self.log_path}")
        print("="*70 + "\n")
        
        start_time = time.time()
        duration_sec = duration_minutes * 60
        poll_sec = 0.5
        
        try:
            while True:
                # Check stop conditions
                elapsed = time.time() - start_time
                
                if windows_target > 0 and len(self.results) >= windows_target:
                    break
                if elapsed >= duration_sec and windows_target == 0:
                    break
                
                current_slug = get_current_window_slug()
                
                # Switch window if needed
                if not self.current_window or self.current_window.slug != current_slug:
                    self._switch_window(current_slug)
                
                if not self.current_window:
                    time.sleep(poll_sec)
                    continue
                
                # Check if window ended
                if self.current_window.is_finished():
                    time.sleep(poll_sec)
                    continue
                
                # Fetch and process tick
                tick = self.fetcher.fetch_tick(self.current_window)
                if tick and tick.ask_up > 0:
                    self.process_tick(tick)
                    
                    # Print status occasionally
                    if self.result and self.result.ticks_seen % 20 == 0:
                        pos = self.position
                        print(f"[{datetime.now().strftime('%H:%M:%S')}] "
                              f"Window {len(self.results)+1} | "
                              f"{tick.seconds_remaining:.0f}s left | "
                              f"Up={tick.ask_up:.2f} Down={tick.ask_down:.2f} | "
                              f"Pos: {pos.q_up:.0f}/{pos.q_down:.0f} | "
                              f"GP: ${pos.guaranteed_profit:.2f}")
                
                time.sleep(poll_sec)
        
        except KeyboardInterrupt:
            print("\n\nStopped by user.")
        
        finally:
            if self.log_file:
                self.log_file.close()
            
            self._print_summary()
            self._save_results()
    
    def _print_summary(self):
        """Print trading summary."""
        print("\n" + "="*70)
        print("PAPER TRADING SUMMARY")
        print("="*70)
        
        print(f"\nWindows traded: {len(self.results)}")
        print(f"Log file: {self.log_path}")
        
        if not self.results:
            print("\nNo complete windows.")
            return
        
        # Aggregate stats
        total_signals_a = sum(r.signals_a for r in self.results)
        total_trades = sum(r.trades_made for r in self.results)
        
        gp_positive = sum(1 for r in self.results 
                        if r.final_position and r.final_position.guaranteed_profit > 0)
        
        print(f"\nVariant A signals seen: {total_signals_a}")
        print(f"Total trades made: {total_trades}")
        print(f"Windows with positive GP: {gp_positive} / {len(self.results)}")
        
        # Per-window summary
        print(f"\nPer-Window Results:")
        print("-"*70)
        
        for r in self.results:
            if r.final_position:
                pos = r.final_position
                print(f"  {r.slug}: trades={r.trades_made}, "
                      f"pos={pos.q_up:.0f}/{pos.q_down:.0f}, "
                      f"cost=${pos.total_cost:.2f}, "
                      f"gp=${pos.guaranteed_profit:.2f}")
    
    def _save_results(self):
        """Save results to file."""
        results_path = self.output_dir / f"results_{datetime.now(timezone.utc).strftime('%Y%m%d_%H%M%S')}.json"
        
        data = {
            "params": {
                "instant_arb_buffer": self.instant_arb_buffer,
                "cheap_cap": self.cheap_cap,
                "clip_size": self.clip_size,
                "profit_target": self.profit_target,
                "stop_buffer_seconds": self.stop_buffer_seconds,
            },
            "results": [r.to_dict() for r in self.results],
        }
        
        with open(results_path, "w") as f:
            json.dump(data, f, indent=2)
        
        print(f"\nResults saved: {results_path}")


def run_paper(duration_minutes: int = 30, windows: int = 0, cheap_cap: float = 0.45):
    """Run paper trading."""
    engine = PaperEngine(cheap_cap=cheap_cap)
    engine.run_continuous(duration_minutes=duration_minutes, windows_target=windows)


if __name__ == "__main__":
    import argparse
    
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s | %(levelname)s | %(message)s",
        datefmt="%H:%M:%S"
    )
    
    parser = argparse.ArgumentParser(description="Paper trade BTC 15-min markets")
    parser.add_argument("--duration", type=int, default=30, help="Duration in minutes")
    parser.add_argument("--windows", type=int, default=0, help="Stop after N windows")
    parser.add_argument("--cheap-cap", type=float, default=0.45, help="Max price to buy")
    parser.add_argument("--clip-size", type=float, default=10, help="Shares per trade")
    parser.add_argument("--profit-target", type=float, default=0.10, help="Profit target $")
    
    args = parser.parse_args()
    
    engine = PaperEngine(
        cheap_cap=args.cheap_cap,
        clip_size=args.clip_size,
        profit_target=args.profit_target,
    )
    engine.run_continuous(duration_minutes=args.duration, windows_target=args.windows)

