"""
Adverse Move Probe - Test Stop-Loss Behavior
=============================================

This script tests the bot's ability to handle adverse price moves:
1. Places a near-touch entry bid
2. Waits for fill
3. Monitors price for adverse move
4. Verifies stop-loss triggers and position is flattened

Required output:
- time-to-flat
- max adverse excursion
- whether emergency flatten used

Usage:
    $env:LIVE="1"
    $env:MM_EXIT_ENFORCED="1"
    $env:MM_STOP_LOSS_CENTS="3"
    $env:MM_EMERGENCY_TAKER_EXIT="1"
    python -u scripts/mm_adverse_move_probe.py --size 5 --timeout 120
"""

import os
import sys
import time
import json
import argparse
from datetime import datetime
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from mm_bot.config import Config, RunMode
from mm_bot.clob import ClobWrapper
from mm_bot.market import MarketResolver
from mm_bot.positions import PositionManager
from mm_bot.exit_supervisor import ExitSupervisor, ExitMode


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--size", type=int, default=5)
    parser.add_argument("--timeout", type=int, default=120)
    parser.add_argument("--stop-loss-cents", type=float, default=3.0)
    parser.add_argument("--outdir", default="mm_probe_out")
    args = parser.parse_args()
    
    live = os.environ.get("LIVE") == "1"
    emergency_enabled = os.environ.get("MM_EMERGENCY_TAKER_EXIT") == "1"
    
    print("=" * 60)
    print("ADVERSE MOVE PROBE")
    print("=" * 60)
    print(f"Mode: {'LIVE' if live else 'DRY RUN'}")
    print(f"Emergency taker exit: {'ENABLED' if emergency_enabled else 'DISABLED'}")
    print(f"Stop-loss: {args.stop_loss_cents}c")
    print(f"Size: {args.size}")
    print("=" * 60)
    
    if not live:
        print("\nSet LIVE=1 to test with real orders")
    
    # Load config
    config = Config.from_env("pm_api_config.json")
    if live:
        config.mode = RunMode.LIVE
    
    # Resolve market
    resolver = MarketResolver(config)
    market = resolver.resolve_market()
    
    if not market:
        print("[ERROR] Could not resolve market")
        return 1
    
    yes_token = market.yes_token_id
    no_token = market.no_token_id
    
    print(f"\n[MARKET] {market.question}")
    print(f"[MARKET] YES: {yes_token[:30]}...")
    print(f"[MARKET] NO: {no_token[:30]}...")
    print(f"[MARKET] Time left: {market.time_str}")
    
    # Initialize components
    clob = ClobWrapper(config)
    position_manager = PositionManager(config)
    position_manager.set_market_tokens(yes_token, no_token)
    
    # Configure stop-loss
    config.risk.stop_loss_cents = args.stop_loss_cents
    config.risk.emergency_taker_exit = True
    
    exit_supervisor = ExitSupervisor(config, clob, position_manager, None)
    
    # Get initial book
    yes_book = clob.get_order_book(yes_token)
    no_book = clob.get_order_book(no_token)
    
    if not yes_book or not yes_book.has_liquidity:
        print("[ERROR] No valid YES book")
        return 1
    
    print(f"\n[BOOK] YES bid={yes_book.best_bid:.4f} ask={yes_book.best_ask:.4f}")
    print(f"[BOOK] NO bid={no_book.best_bid:.4f} ask={no_book.best_ask:.4f}")
    
    # Decide which side to trade (use the one with lower price)
    if yes_book.best_ask <= no_book.best_ask:
        target_token = yes_token
        target_label = "YES"
        target_book = yes_book
    else:
        target_token = no_token
        target_label = "NO"
        target_book = no_book
    
    entry_price = target_book.best_bid  # Join best bid
    
    print(f"\n[ENTRY] Target: {target_label}")
    print(f"[ENTRY] Price: {entry_price:.4f}")
    print(f"[ENTRY] Size: {args.size}")
    print(f"[STOP-LOSS] Trigger at: {entry_price - args.stop_loss_cents/100:.4f}")
    
    if not live:
        print("\n[DRYRUN] Would place entry bid and monitor for adverse move")
        print("[DRYRUN] Set LIVE=1 to test with real orders")
        return 0
    
    # Place entry
    print(f"\n[STEP 1] Placing entry bid...")
    
    try:
        result = clob.post_order(
            token_id=target_token,
            side="BUY",
            price=entry_price,
            size=args.size,
            post_only=True
        )
        
        if not result.success:
            print(f"[ERROR] Failed to place entry: {result.error}")
            return 1
        
        order_id = result.order_id
        print(f"[ENTRY] Order placed: {order_id[:30]}...")
    
    except Exception as e:
        print(f"[ERROR] {e}")
        return 1
    
    # Wait for fill
    print(f"\n[STEP 2] Waiting for fill (up to {args.timeout}s)...")
    
    filled = False
    fill_price = 0.0
    start_time = time.time()
    
    while time.time() - start_time < args.timeout:
        # Reconcile positions
        position_manager.reconcile_from_rest()
        shares = position_manager.get_shares(target_token)
        
        if shares >= args.size * 0.9:  # Allow partial
            filled = True
            pos = position_manager.get_position(target_token)
            fill_price = pos.avg_price if pos else entry_price
            print(f"[FILL] Detected! {shares:.1f} shares @ {fill_price:.4f}")
            break
        
        # Check if order still exists
        orders = clob.get_open_orders()
        order_ids = [o.order_id if hasattr(o, 'order_id') else o.get("id", "") for o in orders]
        if order_id not in order_ids:
            # Order gone but no fill - cancelled or rejected
            if shares < 0.1:
                print("[INFO] Order cancelled/rejected, no fill")
                return 0
        
        time.sleep(1)
        elapsed = time.time() - start_time
        if int(elapsed) % 10 == 0:
            print(f"[WAIT] {int(elapsed)}s elapsed, shares={shares:.1f}")
    
    if not filled:
        print("[INFO] No fill within timeout, cancelling...")
        try:
            clob.cancel_order(order_id)
        except:
            pass
        return 0
    
    # Now monitor for adverse move
    print(f"\n[STEP 3] Monitoring price for adverse move...")
    print(f"[STOP-LOSS] Will trigger if price drops to {fill_price - args.stop_loss_cents/100:.4f}")
    
    max_adverse = 0.0
    position_exit_time = 0.0
    exit_trigger = ""
    
    monitor_start = time.time()
    monitor_timeout = 120  # 2 minutes max
    
    while time.time() - monitor_start < monitor_timeout:
        # Get current book
        book = clob.get_order_book(target_token)
        book_dict = {
            "best_bid": book.best_bid,
            "best_ask": book.best_ask
        }
        
        # Update position MTM
        position_manager.update_mtm(target_token, book.best_bid, book.best_ask)
        
        # Get position
        pos = position_manager.get_position(target_token)
        shares = pos.shares if pos else 0
        
        if shares < 0.1:
            position_exit_time = time.time() - monitor_start
            exit_trigger = "position_flat"
            print(f"[EXIT] Position flat after {position_exit_time:.1f}s")
            break
        
        # Calculate adverse excursion
        adverse = fill_price - book.best_bid
        max_adverse = max(max_adverse, adverse)
        
        # Run exit supervisor
        other_book = {"best_bid": 0.01, "best_ask": 0.99}  # Dummy for other token
        if target_token == yes_token:
            exit_supervisor.tick(book_dict, other_book, yes_token, no_token)
        else:
            exit_supervisor.tick(other_book, book_dict, yes_token, no_token)
        
        # Check exit order
        exit_order = exit_supervisor.get_exit_order(target_token)
        mode_str = exit_order.mode.value if exit_order else "none"
        
        print(f"[MONITOR] bid={book.best_bid:.4f} adverse={adverse*100:.1f}c max={max_adverse*100:.1f}c exit_mode={mode_str} shares={shares:.1f}")
        
        # Check if stop-loss triggered
        if adverse >= args.stop_loss_cents / 100:
            print(f"[STOP-LOSS] TRIGGERED! adverse={adverse*100:.1f}c >= {args.stop_loss_cents}c")
            exit_trigger = "stop_loss"
        
        time.sleep(0.5)
    
    # Final reconcile
    position_manager.reconcile_from_rest()
    final_shares = position_manager.get_shares(target_token)
    
    # Get exit metrics
    exit_metrics = exit_supervisor.get_metrics()
    emergency_used = exit_metrics["emergency_exits"] > 0
    
    # Calculate results
    success = final_shares < 0.1
    
    results = {
        "timestamp": datetime.now().isoformat(),
        "mode": "LIVE" if live else "DRYRUN",
        "target": target_label,
        "entry_price": fill_price,
        "size": args.size,
        "stop_loss_cents": args.stop_loss_cents,
        "emergency_enabled": emergency_enabled,
        "max_adverse_excursion_cents": max_adverse * 100,
        "exit_trigger": exit_trigger or "timeout",
        "time_to_flat_seconds": position_exit_time if position_exit_time > 0 else None,
        "emergency_flatten_used": emergency_used,
        "final_shares": final_shares,
        "success": success,
        "exit_metrics": exit_metrics
    }
    
    # Write results to file
    out_path = Path(args.outdir)
    out_path.mkdir(exist_ok=True)
    with open(out_path / "probe_results.json", "w") as f:
        json.dump(results, f, indent=2)
    
    print("\n" + "=" * 60)
    print("ADVERSE MOVE PROBE RESULTS")
    print("=" * 60)
    print(f"Entry price:              {fill_price:.4f}")
    print(f"Max adverse excursion:    {max_adverse*100:.2f}c")
    print(f"Exit trigger:             {exit_trigger or 'timeout'}")
    print(f"Time to flat:             {position_exit_time:.1f}s" if position_exit_time > 0 else "Time to flat:             NOT FLAT")
    print(f"Emergency flatten used:   {emergency_used}")
    print(f"Final shares:             {final_shares:.1f}")
    print("-" * 60)
    print(f"Exits placed:             {exit_metrics['exits_placed']}")
    print(f"Exits repriced:           {exit_metrics['exits_repriced']}")
    print(f"Exits filled:             {exit_metrics['exits_filled']}")
    print(f"Emergency exits:          {exit_metrics['emergency_exits']}")
    print("=" * 60)
    
    # Clean up any remaining orders
    print("\n[CLEANUP] Cancelling any remaining orders...")
    try:
        clob.cancel_all()
    except:
        pass
    
    if success:
        print("\n[SUCCESS] Position fully exited!")
        print(f"Results saved to: {out_path / 'probe_results.json'}")
    else:
        print("\n[WARNING] Position still exists - manual intervention needed")
    
    return 0 if success else 1


if __name__ == "__main__":
    sys.exit(main())

