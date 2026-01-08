#!/usr/bin/env python3
"""
Continuous Market Making Bot
=============================
Runs the V6 bot continuously across multiple 15-minute windows.
Auto-discovers new windows, handles transitions, dynamic position sizing.

Usage:
    python -u scripts/mm_continuous.py

Environment Variables:
    LIVE=1              - Enable live trading
    MM_EXIT_ENFORCED=1  - Required for safety
    MM_MAX_USDC_LOCKED  - Max USDC per position (auto-adjusted based on balance)
"""

import os
import sys
import time
from datetime import datetime
from pathlib import Path

# Add project root to path
sys.path.insert(0, str(Path(__file__).parent.parent))

from mm_bot.config import Config, RunMode
from mm_bot.clob import ClobWrapper
from mm_bot.market import MarketResolver


def get_account_balance(clob: ClobWrapper) -> float:
    """Get current USDC balance"""
    try:
        bal = clob.get_balance()
        return float(bal.get("usdc", 0))
    except:
        return 0.0


def calculate_position_size(balance: float) -> float:
    """
    Calculate max USDC locked per position based on account balance.
    
    Strategy:
    - Use 15% of account per position (conservative)
    - Min: $1.50, Max: $10.00
    """
    size = balance * 0.15
    size = max(1.50, min(10.00, size))
    return round(size, 2)


def run_single_window(config: Config, resolver: MarketResolver, window_num: int) -> dict:
    """
    Run bot for a single 15-minute window.
    Returns metrics dict.
    """
    from mm_bot.runner_v5 import SafeRunnerV5
    
    # Resolve current market
    market = resolver.resolve_market()
    if not market:
        print(f"[WINDOW {window_num}] No active market found, waiting...", flush=True)
        return {"status": "no_market"}
    
    # Get fresh balance and adjust position size
    clob = ClobWrapper(config)
    balance = get_account_balance(clob)
    max_usdc = calculate_position_size(balance)
    
    print(f"\n{'='*60}", flush=True)
    print(f"[WINDOW {window_num}] Starting: {market.question}", flush=True)
    print(f"[WINDOW {window_num}] Balance: ${balance:.2f}, Max Position: ${max_usdc:.2f}", flush=True)
    print(f"[WINDOW {window_num}] Ends in: {market.time_str}", flush=True)
    print(f"{'='*60}\n", flush=True)
    
    # Update config with dynamic position size
    config.risk.max_usdc_locked = max_usdc
    
    # Create runner
    runner = SafeRunnerV5(
        config=config,
        yes_token=market.yes_token_id,
        no_token=market.no_token_id,
        market_end_time=market.end_time
    )
    
    # Output directory - create if doesn't exist
    out_dir = Path(f"mm_out_continuous/window_{window_num}_{datetime.now().strftime('%H%M')}")
    out_dir.mkdir(parents=True, exist_ok=True)
    
    # Run until window ends (15 min max)
    runner.run(duration_seconds=900, output_dir=str(out_dir))
    
    # Get final balance
    final_balance = get_account_balance(clob)
    pnl = final_balance - balance
    
    return {
        "status": "completed",
        "window": window_num,
        "start_balance": balance,
        "end_balance": final_balance,
        "pnl": pnl,
        "entries": runner.metrics.entries_posted,
        "round_trips": runner.metrics.round_trips
    }


def main():
    """Main continuous loop"""
    print("="*60, flush=True)
    print("  CONTINUOUS MARKET MAKING BOT (V6)", flush=True)
    print("="*60, flush=True)
    print(f"  Started: {datetime.now()}", flush=True)
    print("  Press Ctrl+C to stop", flush=True)
    print("="*60, flush=True)
    
    # Load config
    config = Config.from_env()
    
    if config.mode != RunMode.LIVE:
        print("\n[WARNING] Not in LIVE mode. Set LIVE=1 to trade.", flush=True)
    
    resolver = MarketResolver(config)
    
    window_num = 0
    total_pnl = 0.0
    session_results = []
    
    try:
        while True:
            window_num += 1
            
            try:
                result = run_single_window(config, resolver, window_num)
                session_results.append(result)
                
                if result["status"] == "completed":
                    total_pnl += result.get("pnl", 0)
                    print(f"\n[WINDOW {window_num}] Completed: PnL=${result['pnl']:+.2f}, Total=${total_pnl:+.2f}", flush=True)
                
            except Exception as e:
                print(f"[WINDOW {window_num}] Error: {e}", flush=True)
            
            # Wait for next window (check every 5 seconds)
            print(f"\n[WAITING] Next window... (checking every 5s)", flush=True)
            
            for i in range(60):  # Max 5 min wait
                time.sleep(5)
                
                # Check if new market is available
                try:
                    market = resolver.resolve_market()
                    if market and market.end_time > int(time.time()) + 60:
                        print(f"[NEW WINDOW] Found: {market.question}", flush=True)
                        break
                except:
                    pass
                
                if i % 6 == 0:  # Log every 30s
                    print(f"[WAITING] {i*5}s elapsed...", flush=True)
            
    except KeyboardInterrupt:
        print("\n\n[STOPPED] User interrupt", flush=True)
    
    finally:
        # Print session summary
        print("\n" + "="*60, flush=True)
        print("  SESSION SUMMARY", flush=True)
        print("="*60, flush=True)
        print(f"  Windows: {window_num}", flush=True)
        print(f"  Total PnL: ${total_pnl:+.2f}", flush=True)
        
        completed = [r for r in session_results if r.get("status") == "completed"]
        if completed:
            entries = sum(r.get("entries", 0) for r in completed)
            trips = sum(r.get("round_trips", 0) for r in completed)
            print(f"  Total Entries: {entries}", flush=True)
            print(f"  Total Round Trips: {trips}", flush=True)
        
        print("="*60, flush=True)


if __name__ == "__main__":
    main()

