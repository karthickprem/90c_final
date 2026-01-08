"""
Paper Mode V3 Runner

Uses V3 strategy with:
- Pre-arm / maker-snipe entry
- Probe-then-confirm sizing
- Reversal hazard score
- TP ladder / time-stop / opp-kill exits
- Regime filter

Logs detailed execution stats including:
- Executed trades per hour
- Avg entry ask
- Avg spread at entry  
- Skip reason distribution
- Gap/tail loss distribution
"""

from __future__ import annotations

import argparse
import asyncio
import time
from datetime import datetime
from typing import Optional, List, Dict
from dataclasses import dataclass, field
from collections import Counter

from .strategy_v3 import StrategyV3, V3Config, Tick, State
from .paper_broker import PaperBroker
from .polymarket_client import PolymarketClient, QuoteData
from .logging_utils import TradeLogger
from .config import QuoteMode


@dataclass
class ExecutionStats:
    """Track detailed execution statistics."""
    windows_seen: int = 0
    trades_attempted: int = 0
    trades_executed: int = 0
    pre_arm_triggers: int = 0
    direct_triggers: int = 0
    scaled_up_count: int = 0
    
    # Entry stats
    entry_asks: List[int] = field(default_factory=list)
    entry_spreads: List[int] = field(default_factory=list)
    entry_fills: List[int] = field(default_factory=list)
    
    # Skip reasons
    skip_reasons: Counter = field(default_factory=Counter)
    
    # Exit reasons
    exit_reasons: Counter = field(default_factory=Counter)
    
    # P&L distribution
    pnls: List[float] = field(default_factory=list)
    worst_losses: List[float] = field(default_factory=list)  # < -15%
    
    def record_entry(self, ask: int, spread: int, fill: int) -> None:
        self.entry_asks.append(ask)
        self.entry_spreads.append(spread)
        self.entry_fills.append(fill)
    
    def record_skip(self, reason: str) -> None:
        self.skip_reasons[reason] += 1
    
    def record_exit(self, reason: str, pnl_invested: float) -> None:
        self.exit_reasons[reason] += 1
        self.pnls.append(pnl_invested)
        if pnl_invested <= -0.15:
            self.worst_losses.append(pnl_invested)
    
    def summary(self) -> Dict:
        return {
            'windows_seen': self.windows_seen,
            'trades_attempted': self.trades_attempted,
            'trades_executed': self.trades_executed,
            'pre_arm_triggers': self.pre_arm_triggers,
            'direct_triggers': self.direct_triggers,
            'scaled_up_count': self.scaled_up_count,
            'avg_entry_ask': sum(self.entry_asks) / len(self.entry_asks) if self.entry_asks else 0,
            'avg_entry_spread': sum(self.entry_spreads) / len(self.entry_spreads) if self.entry_spreads else 0,
            'avg_entry_fill': sum(self.entry_fills) / len(self.entry_fills) if self.entry_fills else 0,
            'skip_reasons': dict(self.skip_reasons),
            'exit_reasons': dict(self.exit_reasons),
            'total_pnl': sum(self.pnls),
            'avg_pnl': sum(self.pnls) / len(self.pnls) if self.pnls else 0,
            'win_rate': len([p for p in self.pnls if p > 0]) / len(self.pnls) if self.pnls else 0,
            'gap_count': len(self.worst_losses),
            'worst_loss': min(self.worst_losses) if self.worst_losses else 0,
        }


@dataclass
class CadenceTracker:
    """Track polling cadence."""
    target_interval: float
    samples: List[float] = field(default_factory=list)
    last_tick_time: float = 0
    log_every: int = 60
    
    def record(self, now: float) -> None:
        if self.last_tick_time > 0:
            dt = now - self.last_tick_time
            self.samples.append(dt)
        self.last_tick_time = now
    
    def should_log(self) -> bool:
        return len(self.samples) >= self.log_every
    
    def get_stats(self) -> dict:
        if not self.samples:
            return {}
        return {
            'avg_dt': sum(self.samples) / len(self.samples),
            'min_dt': min(self.samples),
            'max_dt': max(self.samples),
        }
    
    def log_and_reset(self, log_fn) -> None:
        stats = self.get_stats()
        if stats:
            log_fn(f"  [CADENCE] avg={stats['avg_dt']:.3f}s min={stats['min_dt']:.3f}s "
                  f"max={stats['max_dt']:.3f}s (target={self.target_interval}s)")
        self.samples = []


QUOTE_LOG_INTERVAL = 10


class PaperModeV3Runner:
    """V3 Paper Trading Runner with detailed execution stats."""
    
    def __init__(self, config: V3Config, poll_interval: float = 1.0, 
                 bankroll: float = 100.0, outdir: str = "out_paper_v3"):
        self.strategy_config = config
        self.poll_interval = poll_interval
        self.starting_bankroll = bankroll
        self.outdir = outdir
        
        self.client = PolymarketClient()
        self.broker = PaperBroker(starting_bankroll=bankroll)
        self.strategy = StrategyV3(config)
        self.logger = TradeLogger(outdir=outdir)
        self.cadence = CadenceTracker(target_interval=poll_interval)
        self.stats = ExecutionStats()
        
        self.current_window_slug: Optional[str] = None
        self.running = False
        self.tick_count = 0
        self.last_quote: Optional[QuoteData] = None
    
    def log(self, msg: str) -> None:
        ts = datetime.now().strftime("%H:%M:%S.%f")[:-3]
        print(f"[{ts}] {msg}")
    
    async def startup_discovery(self) -> bool:
        self.log("Starting market discovery...")
        
        for attempt in range(10):
            market = await self.client.resolve_btc15_market(force=True)
            if market:
                self.log(f"  Discovered: {market.slug}")
                self.log(f"  UP Token:   {market.up_token_id[:30]}...")
                self.log(f"  DOWN Token: {market.down_token_id[:30]}...")
                return True
            
            self.log(f"  Attempt {attempt + 1}/10: not found, retrying in 30s...")
            await asyncio.sleep(30)
        
        self.log("  Discovery failed")
        return False
    
    async def setup_window(self, slug: str, start_ts: int, end_ts: int) -> None:
        self.log(f"NEW WINDOW: {slug}")
        self.stats.windows_seen += 1
        self.strategy.start_window(slug, float(start_ts), float(end_ts))
        self.current_window_slug = slug
    
    async def handle_window_end(self) -> None:
        if self.strategy.state in [State.IN_POSITION_PROBE, State.IN_POSITION_FULL]:
            self.log("  Window ending - waiting for resolution...")
            
            winner = await self.client.wait_for_resolution(
                self.current_window_slug, max_wait=180
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
                pnl = ctx.realized_pnl_dollars
                pnl_str = f"+${pnl:.2f}" if pnl >= 0 else f"-${abs(pnl):.2f}"
                self.log(f"  SETTLED: {ctx.exit_reason} | {pnl_str}")
                
                self.stats.record_exit(ctx.exit_reason, ctx.realized_pnl_invested)
                self.broker.bankroll += pnl
                self.logger.log_trade(self.strategy.get_trade_record())
        
        elif self.strategy.state == State.DONE:
            # Already done (skipped or exited early)
            pass
        
        else:
            # No trigger
            self.logger.log_skip(
                self.current_window_slug,
                reason='NO_TRIGGER',
                details={'final_state': self.strategy.state.name}
            )
    
    async def process_tick(self) -> Optional[dict]:
        quotes = await self.client.fetch_quotes(use_synthetic=False)
        
        if quotes.up_ask == 0 and quotes.down_ask == 0:
            return None
        
        self.last_quote = quotes
        
        tick = Tick(
            ts=quotes.ts,
            up_bid=quotes.up_bid,
            up_ask=quotes.up_ask,
            up_cents=quotes.up_mid,
            down_bid=quotes.down_bid,
            down_ask=quotes.down_ask,
            down_cents=quotes.down_mid,
            is_synthetic=quotes.is_synthetic,
        )
        
        result = self.strategy.process_tick(tick, self.broker.bankroll)
        return result
    
    def _log_quotes(self, window) -> None:
        quote = self.last_quote
        if not quote:
            return
        
        state = self.strategy.state.name
        mode = "SYN" if quote.is_synthetic else "BOOK"
        
        print(f"\n  [T-{window.secs_left:3d}s] UP: b={quote.up_bid:2d}c a={quote.up_ask:2d}c (sp={quote.up_spread}c) | "
              f"DN: b={quote.down_bid:2d}c a={quote.down_ask:2d}c (sp={quote.down_spread}c) | {mode} | {state}")
    
    async def run(self, duration_hours: float = 2) -> None:
        self.running = True
        loop_start = time.time()
        
        if duration_hours > 0:
            deadline = loop_start + duration_hours * 3600
        else:
            deadline = float('inf')
        
        self._print_banner()
        
        await self.client.connect()
        await self.startup_discovery()
        
        last_window_slug = None
        poll_interval = self.poll_interval
        gap_threshold = 3.0 * poll_interval
        next_poll_target = loop_start + poll_interval
        
        try:
            while self.running and time.time() < deadline:
                tick_start = time.time()
                self.tick_count += 1
                
                # Gap resync
                gap = tick_start - next_poll_target
                if gap > gap_threshold:
                    self.log(f"  [RESYNC] Gap of {gap:.1f}s detected")
                    next_poll_target = tick_start + poll_interval
                
                target_next = next_poll_target
                
                # Window handling
                window = self.client.get_window()
                
                if window.slug != last_window_slug:
                    if last_window_slug is not None:
                        await self.handle_window_end()
                    
                    await self.setup_window(window.slug, window.start_ts, window.end_ts)
                    last_window_slug = window.slug
                
                # Ensure tokens
                if not self.client.tokens:
                    await self.client.ensure_tokens()
                    if not self.client.tokens:
                        sleep_time = max(0, target_next - time.time())
                        if sleep_time > 0:
                            await asyncio.sleep(sleep_time)
                        next_poll_target += poll_interval
                        continue
                
                # Process tick
                result = await self.process_tick()
                
                self.cadence.record(time.time())
                
                if result:
                    self._handle_result(result, window)
                
                # Log quotes periodically in IDLE/PRE_ARM states
                if self.tick_count % QUOTE_LOG_INTERVAL == 0:
                    if self.strategy.state in [State.IDLE, State.PRE_ARM, State.OBSERVE_10S]:
                        self._log_quotes(window)
                
                # Status line
                self._print_status(window)
                
                # Cadence logging
                if self.cadence.should_log():
                    self.cadence.log_and_reset(self.log)
                
                # Sleep
                now = time.time()
                sleep_time = max(0, target_next - now)
                if sleep_time > 0:
                    await asyncio.sleep(sleep_time)
                
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
    
    def _handle_result(self, result: dict, window) -> None:
        ctx = self.strategy.context
        
        if result.get('pre_arm'):
            self.log(f"  PRE-ARM: {result['pre_arm']} [T-{window.secs_left}s]")
            self.stats.pre_arm_triggers += 1
        
        if result.get('triggered'):
            trigger_type = result.get('type', 'TAKER')
            self.log(f"  TRIGGER: {ctx.trigger_side} @ {ctx.trigger_price}c (ASK) "
                    f"[{trigger_type}] [T-{window.secs_left}s]")
            self.stats.trades_attempted += 1
            if trigger_type == 'TAKER':
                self.stats.direct_triggers += 1
        
        if result.get('maker_limit_posted'):
            self.log(f"  MAKER LIMIT: Posted at {result['maker_limit_posted']}c")
        
        if result.get('entry'):
            fill = result.get('fill_price', 0)
            self.log(f"  ENTRY (PROBE): @ {fill}c | Shares: {result.get('probe_shares', 0):.2f}")
            self.stats.trades_executed += 1
            self.stats.record_entry(
                ctx.trigger_price or 0,
                ctx.trigger_spread or 0,
                fill
            )
        
        if result.get('scaled_up'):
            self.log(f"  SCALE UP: Total shares: {result.get('total_shares', 0):.2f}")
            self.stats.scaled_up_count += 1
        
        if result.get('partial_exit'):
            self.log(f"  PARTIAL EXIT @ {result['partial_exit']}c | "
                    f"Remaining: {result.get('remaining', 0):.2f}")
        
        if result.get('exit'):
            reason = result.get('reason', '?')
            pnl = result.get('pnl_dollars', 0)
            pnl_str = f"+${pnl:.2f}" if pnl >= 0 else f"-${abs(pnl):.2f}"
            self.log(f"  EXIT: {reason} @ {result.get('exit_price', 0)}c | {pnl_str}")
            
            self.stats.record_exit(reason, ctx.realized_pnl_invested)
            self.broker.bankroll += pnl
            self.logger.log_trade(self.strategy.get_trade_record())
        
        if result.get('reason') in ['SPIKE_FAIL', 'JUMP_FAIL', 'TIE_SKIP', 'FILL_TIMEOUT',
                                     'REGIME_ALREADY_DECIDED', 'REGIME_WIDE_SPREAD']:
            self.log(f"  SKIP: {result['reason']}")
            self.stats.record_skip(result['reason'])
            self.logger.log_skip(
                self.current_window_slug,
                reason=result['reason'],
                details=self.strategy.get_trade_record(),
            )
    
    def _print_banner(self) -> None:
        cfg = self.strategy_config
        
        print("=" * 70)
        print("PAPER MODE V3 - BTC 15m POLYMARKET STRATEGY")
        print("=" * 70)
        print(f"V3 Strategy Config:")
        print(f"  Pre-arm:    Watch ask >= {cfg.pre_arm_threshold}c, limit at {cfg.limit_entry_price}c")
        print(f"  Trigger:    First ASK touch >= {cfg.trigger_threshold}c")
        print(f"  SPIKE:      min >= {cfg.spike_min}c, max >= {cfg.spike_max}c (10s)")
        print(f"  JUMP:       max delta < {cfg.big_jump}c, mid jumps < {cfg.max_mid_count}")
        print(f"  Hazard:     drawdown <= {cfg.max_drawdown}c, crosses <= {cfg.max_crosses}, slope >= {cfg.min_slope}c/s")
        print(f"  Execution:  p_max={cfg.p_max}c")
        print(f"  Sizing:     probe={cfg.probe_f:.1%}, full={cfg.full_f:.1%}")
        print(f"  TP Ladder:  {cfg.tp_levels}")
        print(f"  SL:         {cfg.sl}c (BID)")
        print(f"  Time-stop:  {cfg.time_stop_secs}s if not >= {cfg.time_stop_target}c")
        print(f"  Opp-kill:   Exit if opp ask >= {cfg.opp_kill_threshold}c within {cfg.opp_kill_within_secs}s")
        print(f"")
        print(f"Paper Config:")
        print(f"  Bankroll:   ${self.starting_bankroll:.2f}")
        print(f"  Poll:       {self.poll_interval}s")
        print(f"  Output:     {self.outdir}/")
        print(f"")
        print(f"QUOTE MODE: BIDASK (realistic)")
        print(f"  Entry uses ASK, Exit uses BID")
        print("=" * 70)
        print("")
    
    def _print_status(self, window) -> None:
        state = self.strategy.state.name
        
        hold_str = ""
        if self.strategy.state in [State.IN_POSITION_PROBE, State.IN_POSITION_FULL]:
            side = self.strategy.context.trigger_side
            entry = self.strategy.context.entry_fill_price
            pos_type = "PROBE" if self.strategy.state == State.IN_POSITION_PROBE else "FULL"
            hold_str = f" | {pos_type} {side} @ {entry}c"
        
        print(f"\r  [{window.time_str}] State: {state:18s}{hold_str} | "
              f"Trades: {self.stats.trades_executed} | "
              f"${self.broker.bankroll:.2f} | tick#{self.tick_count}  ", 
              end="", flush=True)
    
    def _print_final_summary(self) -> None:
        print("\n")
        print("=" * 70)
        print("  FINAL SUMMARY - V3 EXECUTION STATS")
        print("=" * 70)
        
        s = self.stats.summary()
        
        print(f"\nWindows seen:      {s['windows_seen']}")
        print(f"Trades attempted:  {s['trades_attempted']}")
        print(f"Trades executed:   {s['trades_executed']}")
        print(f"Pre-arm triggers:  {self.stats.pre_arm_triggers}")
        print(f"Direct triggers:   {self.stats.direct_triggers}")
        print(f"Scaled up:         {self.stats.scaled_up_count}")
        
        print(f"\nEntry Stats:")
        print(f"  Avg entry ASK:     {s['avg_entry_ask']:.1f}c")
        print(f"  Avg entry spread:  {s['avg_entry_spread']:.1f}c")
        print(f"  Avg entry fill:    {s['avg_entry_fill']:.1f}c")
        
        print(f"\nSkip Reasons:")
        for reason, count in sorted(s['skip_reasons'].items(), key=lambda x: -x[1]):
            print(f"  {reason}: {count}")
        
        print(f"\nExit Reasons:")
        for reason, count in sorted(s['exit_reasons'].items(), key=lambda x: -x[1]):
            print(f"  {reason}: {count}")
        
        print(f"\nP&L Stats:")
        print(f"  Total P&L:     ${sum(self.stats.pnls):.2f}")
        print(f"  Avg P&L/trade: {s['avg_pnl']*100:.2f}%")
        print(f"  Win rate:      {s['win_rate']*100:.1f}%")
        print(f"  Gap count:     {s['gap_count']} (loss <= -15%)")
        print(f"  Worst loss:    {s['worst_loss']*100:.1f}%")
        
        print(f"\nFinal bankroll: ${self.broker.bankroll:.2f}")
        print(f"Results saved to {self.outdir}/")
        print("=" * 70)
        
        # Save stats to file
        import json
        stats_file = f"{self.outdir}/execution_stats.json"
        with open(stats_file, 'w') as f:
            json.dump(s, f, indent=2)


def parse_args():
    parser = argparse.ArgumentParser(description="Paper Mode V3 Runner")
    
    parser.add_argument("--duration", type=float, default=2,
                       help="Duration in hours (0=infinite)")
    parser.add_argument("--poll", type=float, default=1.0,
                       help="Poll interval in seconds")
    parser.add_argument("--outdir", type=str, default="out_paper_v3",
                       help="Output directory")
    parser.add_argument("--bankroll", type=float, default=100.0,
                       help="Starting bankroll")
    
    # V3 config overrides
    parser.add_argument("--pre-arm", type=int, default=86,
                       help="Pre-arm threshold (default: 86)")
    parser.add_argument("--limit-price", type=int, default=90,
                       help="Maker limit price (default: 90)")
    parser.add_argument("--pmax", type=int, default=93,
                       help="Maximum entry price (default: 93)")
    parser.add_argument("--probe-f", type=float, default=0.005,
                       help="Probe size fraction (default: 0.005)")
    parser.add_argument("--full-f", type=float, default=0.02,
                       help="Full size fraction (default: 0.02)")
    
    return parser.parse_args()


def main():
    args = parse_args()
    
    config = V3Config(
        pre_arm_threshold=args.pre_arm,
        limit_entry_price=args.limit_price,
        p_max=args.pmax,
        probe_f=args.probe_f,
        full_f=args.full_f,
    )
    
    runner = PaperModeV3Runner(
        config=config,
        poll_interval=args.poll,
        bankroll=args.bankroll,
        outdir=args.outdir,
    )
    
    asyncio.run(runner.run(duration_hours=args.duration))


if __name__ == "__main__":
    main()


