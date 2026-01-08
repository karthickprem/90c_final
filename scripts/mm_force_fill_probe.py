"""
Force Fill Probe Test
=====================
Place aggressive postOnly bids to force fills, then verify exit path.

Test sequence (repeated N times):
1. Place postOnly bid at best_bid+1tick (top of book)
2. Wait up to 60s for fill
3. If filled:
   - Verify exit order posted within 2s
   - Wait for inventory to return to 0 (max 60s)
4. Record results

This validates the fill→exit path end-to-end.
"""

import sys
import os
import time
import json
from datetime import datetime
from dataclasses import dataclass, field
from typing import List, Optional

# Force unbuffered output
import builtins
_print = builtins.print
def print(*args, **kwargs):
    kwargs['flush'] = True
    _print(*args, **kwargs)

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from mm_bot.config import Config, RunMode
from mm_bot.clob import ClobWrapper, OrderBook, Side
from mm_bot.market import MarketResolver
from mm_bot.inventory import InventoryManager
from mm_bot.order_manager import OrderManager, OrderRole
from mm_bot.balance import BalanceManager
from mm_bot.quoting import Quote


@dataclass
class ProbeResult:
    """Result of a single probe"""
    probe_num: int
    entry_price: float
    entry_size: float
    filled: bool = False
    fill_time_ms: float = 0.0
    exit_posted: bool = False
    exit_post_latency_ms: float = 0.0
    exit_filled: bool = False
    exit_time_ms: float = 0.0
    inventory_cleared: bool = False
    error: Optional[str] = None


@dataclass
class ProbeReport:
    """Overall probe results"""
    probes: List[ProbeResult] = field(default_factory=list)
    
    @property
    def total(self) -> int:
        return len(self.probes)
    
    @property
    def fills(self) -> int:
        return sum(1 for p in self.probes if p.filled)
    
    @property
    def exits_posted(self) -> int:
        return sum(1 for p in self.probes if p.exit_posted)
    
    @property
    def exits_filled(self) -> int:
        return sum(1 for p in self.probes if p.exit_filled)
    
    @property
    def inventory_cleared_count(self) -> int:
        return sum(1 for p in self.probes if p.inventory_cleared)
    
    def to_report(self) -> str:
        lines = [
            "=" * 60,
            "FORCE FILL PROBE REPORT",
            "=" * 60,
            f"Total probes:        {self.total}",
            f"Entry fills:         {self.fills}",
            f"Exit orders posted:  {self.exits_posted}",
            f"Exit fills:          {self.exits_filled}",
            f"Inventory cleared:   {self.inventory_cleared_count}",
            "",
        ]
        
        if self.fills > 0:
            fill_times = [p.fill_time_ms for p in self.probes if p.filled]
            exit_latencies = [p.exit_post_latency_ms for p in self.probes if p.exit_posted]
            
            lines.append(f"Avg fill time:       {sum(fill_times)/len(fill_times):.0f}ms")
            if exit_latencies:
                lines.append(f"Avg exit latency:    {sum(exit_latencies)/len(exit_latencies):.0f}ms")
        
        lines.append("")
        lines.append("--- INDIVIDUAL PROBES ---")
        for p in self.probes:
            status = "FILL" if p.filled else "NO_FILL"
            if p.filled:
                status += f" -> EXIT_POST:{p.exit_post_latency_ms:.0f}ms"
                if p.exit_filled:
                    status += f" -> EXIT_FILL:{p.exit_time_ms:.0f}ms"
                if p.inventory_cleared:
                    status += " -> CLEARED"
            if p.error:
                status += f" ERROR:{p.error}"
            lines.append(f"  Probe {p.probe_num}: {p.entry_price:.2f} x {p.entry_size} -> {status}")
        
        lines.append("=" * 60)
        
        # Verdict
        if self.fills == 0:
            lines.append("VERDICT: NO FILLS - Market not taking our bids")
        elif self.inventory_cleared_count == self.fills:
            lines.append("VERDICT: PASS - All fills properly exited")
        else:
            lines.append(f"VERDICT: PARTIAL - {self.inventory_cleared_count}/{self.fills} properly exited")
        
        lines.append("=" * 60)
        return "\n".join(lines)


def log(event: str, msg: str):
    ts = datetime.now().strftime("%H:%M:%S.%f")[:-3]
    print(f"[{ts}] [{event}] {msg}")


def run_probe(config: Config, probe_num: int, quote_size: int = 5) -> ProbeResult:
    """Run a single probe"""
    result = ProbeResult(probe_num=probe_num, entry_price=0, entry_size=quote_size)
    
    clob = ClobWrapper(config)
    resolver = MarketResolver(config)
    inventory = InventoryManager(config)
    order_manager = OrderManager(config, clob)
    balance_mgr = BalanceManager(clob, config)
    
    # Resolve market
    market = resolver.resolve_market()
    if not market:
        result.error = "No market"
        return result
    
    inventory.set_tokens(market.yes_token_id, market.no_token_id)
    
    # Get book
    yes_book = clob.get_order_book(market.yes_token_id)
    if not yes_book or not yes_book.has_liquidity:
        result.error = "No liquidity"
        return result
    
    # Calculate aggressive bid (at top of book or improve by 1 tick)
    entry_price = yes_book.best_bid + 0.01  # Improve by 1 tick
    entry_price = min(entry_price, yes_book.best_ask - 0.01)  # Don't cross
    entry_price = round(entry_price, 2)
    result.entry_price = entry_price
    
    log("PROBE", f"#{probe_num}: Placing BID @ {entry_price:.2f} x {quote_size} (best_bid={yes_book.best_bid:.2f} best_ask={yes_book.best_ask:.2f})")
    
    # Place entry bid
    entry_result = clob.post_order(
        token_id=market.yes_token_id,
        side=Side.BUY,
        price=entry_price,
        size=quote_size,
        post_only=True
    )
    
    if not entry_result.success:
        result.error = f"Entry failed: {entry_result.error}"
        log("ERROR", result.error)
        return result
    
    entry_order_id = entry_result.order_id
    entry_time = time.time()
    
    # Wait for fill (max 60s)
    log("WAIT", f"Waiting for fill (max 60s)...")
    fill_detected = False
    
    for i in range(60):
        time.sleep(1)
        
        orders = clob.get_open_orders()
        our_order = None
        for o in orders:
            if o.order_id == entry_order_id or (o.token_id == market.yes_token_id and o.side == "BUY"):
                our_order = o
                break
        
        if our_order and our_order.size_matched > 0:
            fill_detected = True
            result.filled = True
            result.fill_time_ms = (time.time() - entry_time) * 1000
            log("FILL", f"Got fill: {our_order.size_matched:.1f} @ {entry_price:.2f} in {result.fill_time_ms:.0f}ms")
            inventory.process_fill(market.yes_token_id, "BUY", our_order.size_matched, entry_price)
            break
        
        if not our_order:
            # Order gone - either filled entirely or cancelled
            balance = clob.get_balance()
            if balance["positions"] > 0.01:
                fill_detected = True
                result.filled = True
                result.fill_time_ms = (time.time() - entry_time) * 1000
                log("FILL", f"Order gone, position exists - fill in {result.fill_time_ms:.0f}ms")
                inventory.process_fill(market.yes_token_id, "BUY", quote_size, entry_price)
                break
        
        if (i + 1) % 10 == 0:
            log("WAIT", f"Still waiting... {i+1}s")
    
    # Cancel entry if not filled
    if not fill_detected:
        log("NO_FILL", f"No fill after 60s, cancelling")
        clob.cancel_order(entry_order_id)
        return result
    
    # FILL DETECTED - now verify exit path
    exit_start = time.time()
    
    # Place exit order
    yes_book = clob.get_order_book(market.yes_token_id)
    exit_price = yes_book.best_ask - 0.01 if yes_book else entry_price + 0.01
    exit_price = round(exit_price, 2)
    
    inv = inventory.get_yes_shares()
    log("EXIT", f"Placing exit SELL @ {exit_price:.2f} x {inv:.1f}")
    
    quote = Quote(price=exit_price, size=inv, side="SELL")
    exit_result = order_manager.place_or_replace(market.yes_token_id, quote, role=OrderRole.EXIT)
    
    if exit_result and exit_result.success:
        result.exit_posted = True
        result.exit_post_latency_ms = (time.time() - exit_start) * 1000
        log("EXIT_POSTED", f"Exit order posted in {result.exit_post_latency_ms:.0f}ms")
    else:
        result.error = "Exit order failed"
        return result
    
    # Wait for exit fill (max 60s)
    log("WAIT", "Waiting for exit fill...")
    exit_wait_start = time.time()
    
    for i in range(60):
        time.sleep(1)
        
        # Check positions
        balance = clob.get_balance()
        if balance["positions"] <= 0.01:
            result.exit_filled = True
            result.exit_time_ms = (time.time() - exit_wait_start) * 1000
            result.inventory_cleared = True
            log("EXIT_FILL", f"Position cleared in {result.exit_time_ms:.0f}ms")
            break
        
        # Reprice exit if needed (every 3s)
        if (i + 1) % 3 == 0:
            yes_book = clob.get_order_book(market.yes_token_id)
            if yes_book:
                new_exit_price = round(yes_book.best_ask - 0.01, 2)
                if new_exit_price != exit_price:
                    exit_price = new_exit_price
                    log("EXIT_REPRICE", f"Repricing to {exit_price:.2f}")
                    order_manager.cancel_all()
                    quote = Quote(price=exit_price, size=inv, side="SELL")
                    order_manager.place_or_replace(market.yes_token_id, quote, role=OrderRole.EXIT)
        
        if (i + 1) % 10 == 0:
            log("WAIT", f"Exit wait... {i+1}s, positions=${balance['positions']:.2f}")
    
    # Final cleanup
    clob.cancel_all()
    
    return result


def main():
    import argparse
    
    parser = argparse.ArgumentParser(description="Force Fill Probe Test")
    parser.add_argument("--probes", type=int, default=10, help="Number of probes")
    parser.add_argument("--size", type=int, default=5, help="Quote size")
    args = parser.parse_args()
    
    if os.environ.get("LIVE") != "1":
        print("[ERROR] Requires LIVE=1")
        return
    
    config = Config.from_env("pm_api_config.json")
    config.mode = RunMode.LIVE
    
    print("=" * 60)
    print("FORCE FILL PROBE TEST")
    print("=" * 60)
    print(f"Probes: {args.probes}")
    print(f"Size: {args.size}")
    print("=" * 60)
    
    report = ProbeReport()
    
    for i in range(args.probes):
        log("PROBE_START", f"Starting probe {i+1}/{args.probes}")
        
        result = run_probe(config, i+1, args.size)
        report.probes.append(result)
        
        # Brief pause between probes
        if i < args.probes - 1:
            log("PAUSE", "Waiting 5s before next probe...")
            time.sleep(5)
    
    # Print final report
    print("\n" + report.to_report())
    
    # Save report
    with open("probe_report.txt", "w") as f:
        f.write(report.to_report())
    
    with open("probe_results.json", "w") as f:
        json.dump([{
            "probe_num": p.probe_num,
            "entry_price": p.entry_price,
            "filled": p.filled,
            "fill_time_ms": p.fill_time_ms,
            "exit_posted": p.exit_posted,
            "exit_post_latency_ms": p.exit_post_latency_ms,
            "exit_filled": p.exit_filled,
            "inventory_cleared": p.inventory_cleared,
            "error": p.error
        } for p in report.probes], f, indent=2)
    
    print(f"\nReport saved to probe_report.txt")


if __name__ == "__main__":
    main()

