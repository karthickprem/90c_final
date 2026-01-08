"""
Paper Mode Runner

Main entry point for paper trading.
Orchestrates:
- Precise polling with monotonic timing
- Polymarket API via robust client
- Strategy state machine
- Paper broker for fills
- Trade logging with cadence tracking
"""

from __future__ import annotations

import argparse
import asyncio
import time
from datetime import datetime
from typing import Optional, List
from dataclasses import dataclass, field

from .config import StrategyConfig, PaperConfig, QuoteMode
from .strategy import StrategyStateMachine, Tick, State
from .paper_broker import PaperBroker
from .polymarket_client import PolymarketClient, QuoteData
from .logging_utils import TradeLogger


@dataclass
class CadenceTracker:
    """Track polling cadence for diagnostics."""
    target_interval: float
    samples: List[float] = field(default_factory=list)
    last_tick_time: float = 0
    log_every: int = 60  # Log every N ticks
    
    def record(self, now: float) -> None:
        """Record a tick."""
        if self.last_tick_time > 0:
            dt = now - self.last_tick_time
            self.samples.append(dt)
        self.last_tick_time = now
    
    def should_log(self) -> bool:
        """Check if we should log cadence."""
        return len(self.samples) >= self.log_every
    
    def get_stats(self) -> dict:
        """Get cadence statistics."""
        if not self.samples:
            return {}
        
        avg_dt = sum(self.samples) / len(self.samples)
        min_dt = min(self.samples)
        max_dt = max(self.samples)
        
        return {
            'avg_dt': avg_dt,
            'min_dt': min_dt,
            'max_dt': max_dt,
            'samples': len(self.samples),
            'slow': avg_dt > self.target_interval * 1.5,
        }
    
    def log_and_reset(self, log_fn) -> None:
        """Log stats and reset."""
        stats = self.get_stats()
        if stats:
            log_fn(f"  [CADENCE] avg={stats['avg_dt']:.3f}s min={stats['min_dt']:.3f}s "
                  f"max={stats['max_dt']:.3f}s (target={self.target_interval}s)")
            if stats['slow']:
                log_fn("  [WARNING] Polling slower than configured!")
        self.samples = []


# Price visibility settings
PRICE_LOG_INTERVAL = 10  # Log prices every N ticks


class PaperModeRunner:
    """
    Main paper trading runner.
    
    Features:
    - Precise monotonic polling with gap resync
    - Robust market discovery
    - Bid/ask quote support (not just midpoint)
    - Cadence tracking and logging
    - No real orders placed
    """
    
    def __init__(self, config: PaperConfig):
        self.config = config
        self.client = PolymarketClient()
        self.broker = PaperBroker(starting_bankroll=config.starting_bankroll)
        self.strategy = StrategyStateMachine(config.strategy)
        self.logger = TradeLogger(outdir=config.outdir)
        self.cadence = CadenceTracker(target_interval=config.poll_interval_secs)
        
        # Runtime state
        self.current_window_slug: Optional[str] = None
        self.running = False
        self.tick_count = 0
        
        # Last quote data (bid/ask/mid for both sides)
        self.last_quote: Optional[QuoteData] = None
    
    def log(self, msg: str) -> None:
        """Print timestamped message."""
        ts = datetime.now().strftime("%H:%M:%S.%f")[:-3]
        print(f"[{ts}] {msg}")
    
    async def startup_discovery(self) -> bool:
        """
        Run market discovery on startup.
        
        Keep retrying until market is found or timeout.
        """
        self.log("Starting market discovery...")
        
        max_attempts = 10
        for attempt in range(max_attempts):
            market = await self.client.resolve_btc15_market(force=True)
            if market:
                self.log(f"  Discovered: {market.slug}")
                self.log(f"  UP Token:   {market.up_token_id[:30]}...")
                self.log(f"  DOWN Token: {market.down_token_id[:30]}...")
                return True
            
            self.log(f"  Attempt {attempt + 1}/{max_attempts}: not found, retrying in 30s...")
            await asyncio.sleep(30)
        
        self.log("  Discovery failed after max attempts")
        self.log("  Will continue and retry during polling")
        return False
    
    async def setup_window(self, slug: str, start_ts: int, end_ts: int) -> bool:
        """Initialize for a new window."""
        self.log(f"NEW WINDOW: {slug}")
        
        # Log cached token status (don't re-discover on window change)
        if self.client.tokens:
            self.log(f"  Using cached tokens")
        else:
            self.log("  No tokens cached - will discover on next tick")
        
        # Initialize strategy for this window
        self.strategy.start_window(slug, float(start_ts), float(end_ts))
        self.current_window_slug = slug
        
        return True
    
    async def handle_window_end(self) -> None:
        """Handle end of window - settle position if open."""
        if self.strategy.state == State.IN_POSITION:
            self.log("  Window ending - waiting for resolution...")
            
            winner = await self.client.wait_for_resolution(
                self.current_window_slug,
                max_wait=180
            )
            
            if winner:
                self.log(f"  Winner: {winner.upper()}")
                winner_side = winner.upper()
            else:
                self.log("  Resolution timeout - assuming loss")
                our_side = self.strategy.context.trigger_side
                winner_side = 'DOWN' if our_side == 'UP' else 'UP'
            
            result = self.strategy.force_settle(winner_side)
            
            if result.get('exit'):
                ctx = self.strategy.context
                
                trade = self.broker.close_position(
                    window_id=ctx.window_id,
                    exit_price=ctx.exit_price,
                    exit_reason=ctx.exit_reason,
                )
                
                if trade:
                    pnl_str = f"+${trade.pnl_dollars:.2f}" if trade.pnl_dollars >= 0 else f"-${abs(trade.pnl_dollars):.2f}"
                    self.log(f"  SETTLED: {ctx.exit_reason} | {pnl_str} | Bankroll: ${self.broker.bankroll:.2f}")
                    self.logger.log_trade(self.strategy.get_trade_record())
        
        elif self.strategy.state == State.DONE:
            pass
        
        else:
            self.logger.log_skip(
                self.current_window_slug,
                reason='NO_TRIGGER',
                details={'final_state': self.strategy.state.name}
            )
    
    async def process_tick(self) -> Optional[dict]:
        """
        Fetch current quotes (bid/ask) and feed to strategy.
        
        Uses quote_mode from config:
        - BIDASK: fetch real orderbook bid/ask (required for realistic simulation)
        - MID: fallback to midpoint (NOT decision-grade)
        """
        use_synthetic = self.config.quote_mode == QuoteMode.MID
        
        quotes = await self.client.fetch_quotes(
            use_synthetic=use_synthetic,
            default_spread=self.config.synthetic_spread
        )
        
        # Validate we got data
        if quotes.up_ask == 0 and quotes.down_ask == 0:
            return None
        
        # Track last quote for visibility
        self.last_quote = quotes
        
        # Build tick with full bid/ask data
        tick = Tick(
            ts=quotes.ts,
            up_bid=quotes.up_bid,
            up_ask=quotes.up_ask,
            up_cents=quotes.up_mid,  # Keep mid for logging
            down_bid=quotes.down_bid,
            down_ask=quotes.down_ask,
            down_cents=quotes.down_mid,
            is_synthetic=quotes.is_synthetic,
        )
        
        result = self.strategy.process_tick(tick, self.broker.bankroll)
        return result
    
    def _should_log_prices(self) -> bool:
        """Check if we should log prices this tick."""
        return self.tick_count % PRICE_LOG_INTERVAL == 0
    
    def _log_quotes(self, window) -> None:
        """Log current bid/ask quotes with spread info."""
        state = self.strategy.state.name
        quote = self.last_quote
        
        if not quote:
            return
        
        mode_str = "SYN" if quote.is_synthetic else "BOOK"
        
        print(f"\n  [T-{window.secs_left:3d}s] UP: bid={quote.up_bid:2d}c ask={quote.up_ask:2d}c (sp={quote.up_spread}c) | "
              f"DOWN: bid={quote.down_bid:2d}c ask={quote.down_ask:2d}c (sp={quote.down_spread}c) | {mode_str} | {state}")
        
        # Log which prices are used
        print(f"           Entry uses: ASK | Exit uses: BID", end="")
    
    async def handle_entry(self) -> None:
        """Handle entry fill in broker."""
        ctx = self.strategy.context
        
        position = self.broker.open_position(
            window_id=ctx.window_id,
            side=ctx.trigger_side,
            fill_price=ctx.entry_fill_price,
            f=self.config.strategy.f,
        )
        
        if position:
            self.log(f"  ENTRY: {ctx.trigger_side} @ {ctx.entry_fill_price}c | "
                    f"Shares: {position.shares:.2f} | Cost: ${position.cost:.2f}")
    
    async def handle_exit(self) -> None:
        """Handle exit in broker."""
        ctx = self.strategy.context
        
        trade = self.broker.close_position(
            window_id=ctx.window_id,
            exit_price=ctx.exit_price,
            exit_reason=ctx.exit_reason,
        )
        
        if trade:
            pnl_str = f"+${trade.pnl_dollars:.2f}" if trade.pnl_dollars >= 0 else f"-${abs(trade.pnl_dollars):.2f}"
            self.log(f"  EXIT: {ctx.exit_reason} @ {ctx.exit_price}c | {pnl_str} | Bankroll: ${self.broker.bankroll:.2f}")
            self.logger.log_trade(self.strategy.get_trade_record())
    
    async def run(self, duration_hours: float = 12) -> None:
        """
        Main run loop with precise monotonic polling.
        
        Args:
            duration_hours: How long to run (0 = infinite)
        """
        self.running = True
        loop_start_time = time.time()
        
        if duration_hours > 0:
            deadline = loop_start_time + duration_hours * 3600
        else:
            deadline = float('inf')
        
        self._print_banner()
        
        await self.client.connect()
        
        # Initial discovery
        await self.startup_discovery()
        
        last_window_slug = None
        poll_interval = self.config.poll_interval_secs
        gap_threshold = self.config.gap_resync_threshold * poll_interval
        
        # Track next target poll time for gap resync
        next_poll_target = loop_start_time + poll_interval
        
        try:
            while self.running and time.time() < deadline:
                tick_start = time.time()
                self.tick_count += 1
                
                # GAP RESYNC: If we've been sleeping too long, resync instead of catching up
                gap = tick_start - next_poll_target
                if gap > gap_threshold:
                    self.log(f"  [RESYNC] Gap of {gap:.1f}s detected (>{gap_threshold:.1f}s) - resyncing scheduler")
                    # Resync: set next target to now + poll_interval (don't catch up)
                    next_poll_target = tick_start + poll_interval
                
                # Calculate target time for next tick (monotonic but with resync)
                target_next = next_poll_target
                
                # Get current window
                window = self.client.get_window()
                
                # Window transition
                if window.slug != last_window_slug:
                    if last_window_slug is not None:
                        await self.handle_window_end()
                    
                    await self.setup_window(
                        window.slug,
                        window.start_ts,
                        window.end_ts,
                    )
                    last_window_slug = window.slug
                
                # Ensure we have tokens
                if not self.client.tokens:
                    await self.client.ensure_tokens()
                    if not self.client.tokens:
                        # Sleep to next target and continue
                        sleep_time = max(0, target_next - time.time())
                        if sleep_time > 0:
                            await asyncio.sleep(sleep_time)
                        continue
                
                # Process tick
                result = await self.process_tick()
                
                # Record cadence
                self.cadence.record(time.time())
                
                if result:
                    if result.get('triggered'):
                        self.log(f"  TRIGGER: {self.strategy.context.trigger_side} @ "
                                f"{self.strategy.context.trigger_price}c (ASK) [T-{window.secs_left}s]")
                    
                    if result.get('entry'):
                        await self.handle_entry()
                    
                    if result.get('exit'):
                        await self.handle_exit()
                    
                    if result.get('reason') in ['SPIKE_FAIL', 'JUMP_FAIL', 'TIE_SKIP', 'FILL_TIMEOUT']:
                        self.log(f"  SKIP: {result.get('reason')}")
                        self.logger.log_skip(
                            self.current_window_slug,
                            reason=result.get('reason'),
                            details=self.strategy.get_trade_record(),
                        )
                
                # Log quotes every N ticks while in IDLE/OBSERVE states
                if self._should_log_prices() and self.strategy.state in [State.IDLE, State.OBSERVE_10S]:
                    self._log_quotes(window)
                
                # Status line
                self._print_status(window)
                
                # Log cadence periodically
                if self.cadence.should_log():
                    self.cadence.log_and_reset(self.log)
                
                # Precise sleep to target (monotonic scheduling with gap resync)
                now = time.time()
                sleep_time = max(0, target_next - now)
                if sleep_time > 0:
                    await asyncio.sleep(sleep_time)
                
                # Advance next poll target monotonically
                next_poll_target += poll_interval
        
        except KeyboardInterrupt:
            self.log("\n\nStopped by user")
        
        except Exception as e:
            self.log(f"\n\nERROR: {e}")
            import traceback
            traceback.print_exc()
        
        finally:
            if last_window_slug:
                await self.handle_window_end()
            
            await self.client.close()
            self._print_final_summary()
    
    def _print_banner(self) -> None:
        """Print startup banner."""
        cfg = self.config.strategy
        
        print("=" * 70)
        print("PAPER MODE - BTC 15m POLYMARKET STRATEGY")
        print("=" * 70)
        print(f"Strategy Config:")
        print(f"  Trigger:    First ASK touch >= {cfg.trigger_threshold}c")
        print(f"  SPIKE:      min >= {cfg.spike_min}c, max >= {cfg.spike_max}c (10s window, ASK)")
        print(f"  JUMP:       max delta < {cfg.big_jump}c, mid jumps < {cfg.max_mid_count} (ASK)")
        print(f"  Execution:  p_max={cfg.p_max}c, slip_entry={cfg.slip_entry}c (on ASK)")
        print(f"  TP/SL:      TP={cfg.tp}c (BID), SL={cfg.sl}c (BID), slip_exit={cfg.slip_exit}c")
        print(f"  Sizing:     f={cfg.f:.1%}")
        print(f"")
        print(f"Paper Config:")
        print(f"  Bankroll:   ${self.config.starting_bankroll:.2f}")
        print(f"  Poll:       {self.config.poll_interval_secs}s (monotonic + gap resync)")
        print(f"  Quote Mode: {self.config.quote_mode.value.upper()}")
        print(f"  Output:     {self.config.outdir}/")
        print(f"")
        if self.config.quote_mode == QuoteMode.BIDASK:
            print(f"QUOTE MODE: BIDASK (realistic tradable prices)")
            print(f"  Entry trigger/fill uses ASK (price you pay to buy)")
            print(f"  Exit TP/SL uses BID (price you receive when selling)")
        else:
            print(f"WARNING: QUOTE MODE = MID (not decision-grade for deployment)")
            print(f"  Run with --quote-mode bidask for realistic simulation")
        print("=" * 70)
        print("")
    
    def _print_status(self, window) -> None:
        """Print status line."""
        state = self.strategy.state.name
        
        hold_str = ""
        if self.strategy.state == State.IN_POSITION:
            side = self.strategy.context.trigger_side
            entry = self.strategy.context.entry_fill_price
            hold_str = f" | HOLD {side} @ {entry}c"
        
        stats = self.broker.get_stats()
        
        print(f"\r  [{window.time_str}] State: {state:15s}{hold_str} | "
              f"W{stats.get('wins', 0)}/L{stats.get('losses', 0)} | "
              f"${self.broker.bankroll:.2f} | tick#{self.tick_count}  ", 
              end="", flush=True)
    
    def _print_final_summary(self) -> None:
        """Print and save final summary."""
        stats = self.broker.get_stats()
        
        self.logger.write_daily_summary(stats)
        self.logger.print_summary(stats)
        
        # Final cadence summary
        cadence_stats = self.cadence.get_stats()
        if cadence_stats:
            print(f"\nFinal cadence: avg={cadence_stats['avg_dt']:.3f}s "
                  f"(target={self.config.poll_interval_secs}s)")
        
        print(f"\nResults saved to {self.config.outdir}/")


def parse_args():
    """Parse command line arguments."""
    parser = argparse.ArgumentParser(
        description="Paper Mode for BTC 15m Polymarket Strategy"
    )
    
    parser.add_argument("--duration", type=float, default=12,
                       help="Duration to run in hours (0=infinite, default: 12)")
    parser.add_argument("--poll", type=float, default=1.0,
                       help="Poll interval in seconds (default: 1.0)")
    parser.add_argument("--outdir", type=str, default="out_paper",
                       help="Output directory (default: out_paper)")
    
    parser.add_argument("--bankroll", type=float, default=100.0,
                       help="Starting paper bankroll (default: 100)")
    parser.add_argument("--f", type=float, default=0.02,
                       help="Fraction of bankroll per trade (default: 0.02)")
    
    parser.add_argument("--pmax", type=int, default=93,
                       help="Maximum entry price in cents (default: 93)")
    parser.add_argument("--slip-entry", type=int, default=1,
                       help="Entry slippage in cents (default: 1)")
    parser.add_argument("--slip-exit", type=int, default=1,
                       help="Exit slippage in cents (default: 1)")
    parser.add_argument("--tp", type=int, default=97,
                       help="Take profit threshold in cents (default: 97)")
    parser.add_argument("--sl", type=int, default=86,
                       help="Stop loss threshold in cents (default: 86)")
    
    # Quote mode (CRITICAL for realistic simulation)
    parser.add_argument("--quote-mode", type=str, default="bidask",
                       choices=["bidask", "mid"],
                       help="Price quote mode: bidask (default, realistic) or mid (NOT decision-grade)")
    parser.add_argument("--allow-mid", action="store_true",
                       help="Allow running in mid mode (required if --quote-mode mid)")
    parser.add_argument("--synthetic-spread", type=int, default=2,
                       help="Default spread in cents for synthetic quotes (default: 2)")
    
    return parser.parse_args()


def main():
    """Main entry point."""
    args = parse_args()
    
    # Parse quote mode
    quote_mode = QuoteMode.BIDASK if args.quote_mode == "bidask" else QuoteMode.MID
    
    strategy_config = StrategyConfig(
        p_max=args.pmax,
        slip_entry=args.slip_entry,
        slip_exit=args.slip_exit,
        tp=args.tp,
        sl=args.sl,
        f=args.f,
    )
    
    config = PaperConfig(
        poll_interval_secs=args.poll,
        starting_bankroll=args.bankroll,
        outdir=args.outdir,
        quote_mode=quote_mode,
        allow_mid=args.allow_mid,
        synthetic_spread=args.synthetic_spread,
        strategy=strategy_config,
    )
    
    runner = PaperModeRunner(config)
    asyncio.run(runner.run(duration_hours=args.duration))


if __name__ == "__main__":
    main()
