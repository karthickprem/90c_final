#!/usr/bin/env python3
"""
Rebate Farming Bot - Continuous Runner
=======================================
Runs the V7 rebate farming bot continuously across multiple windows.

Strategy:
- MIDDLE ZONE (0.30-0.70): Quote both sides, exit at +1-2c or scratch
- EXTREME ZONE (>0.90): Quote dominant side, exit fast

Goal: Earn maker rebates, not directional profits.

Usage:
    $env:LIVE="1"; $env:MM_EXIT_ENFORCED="1"; python -u scripts/mm_rebate_farmer.py
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


def get_balance(clob: ClobWrapper) -> float:
    """Get current USDC balance"""
    try:
        bal = clob.get_balance()
        return float(bal.get("usdc", 0))
    except:
        return 0.0


def calculate_max_position(balance: float) -> float:
    """
    Calculate max USDC per position.
    For rebate farming: 25% of balance (need enough for 5 shares at 50c = $2.50)
    Min $2.50, Max $10
    """
    size = balance * 0.25
    return max(2.50, min(10.00, round(size, 2)))


def run_single_window(config: Config, resolver: MarketResolver, window_num: int) -> dict:
    """Run V7 bot for a single window"""
    from mm_bot.runner_v7 import RebateFarmingBot
    
    # Resolve market
    market = resolver.resolve_market()
    if not market:
        print(f"[WINDOW {window_num}] No active market", flush=True)
        return {"status": "no_market"}
    
    # Get balance
    clob = ClobWrapper(config)
    balance = get_balance(clob)
    max_pos = calculate_max_position(balance)
    
    print(f"\n{'='*60}", flush=True)
    print(f"[WINDOW {window_num}] {market.question}", flush=True)
    print(f"[WINDOW {window_num}] Balance: ${balance:.2f}, Max Position: ${max_pos:.2f}", flush=True)
    print(f"[WINDOW {window_num}] Ends in: {market.time_str}", flush=True)
    print(f"{'='*60}\n", flush=True)
    
    # Update config
    config.risk.max_usdc_locked = max_pos
    
    # Create bot
    bot = RebateFarmingBot(
        config=config,
        yes_token=market.yes_token_id,
        no_token=market.no_token_id,
        market_end_time=market.end_time
    )
    
    # Output directory
    out_dir = Path(f"mm_out_v7/window_{window_num}_{datetime.now().strftime('%H%M')}")
    out_dir.mkdir(parents=True, exist_ok=True)
    
    # Run (15 min max)
    bot.run(duration_seconds=900, output_dir=str(out_dir))
    
    # Get results
    final_balance = get_balance(clob)
    pnl = final_balance - balance
    
    return {
        "status": "completed",
        "window": window_num,
        "start_balance": balance,
        "end_balance": final_balance,
        "pnl": pnl,
        "fills": bot.metrics.entries_filled,
        "trade_pnl_cents": bot.metrics.total_pnl_cents,
        "scratches": bot.metrics.scratches,
        "stops": bot.metrics.stop_losses
    }


def main():
    """Main continuous loop"""
    print("=" * 60, flush=True)
    print("  V7 REBATE FARMING BOT", flush=True)
    print("=" * 60, flush=True)
    print(f"  Started: {datetime.now()}", flush=True)
    print("  Strategy: Get filled -> Collect rebate -> Exit fast", flush=True)
    print("  Press Ctrl+C to stop", flush=True)
    print("=" * 60, flush=True)
    
    # Load config
    config = Config.from_env()
    
    if config.mode != RunMode.LIVE:
        print("\n[WARNING] Not in LIVE mode. Set LIVE=1 to trade.", flush=True)
    
    resolver = MarketResolver(config)
    
    window_num = 0
    total_pnl = 0.0
    total_fills = 0
    session_results = []
    
    try:
        while True:
            window_num += 1
            
            try:
                result = run_single_window(config, resolver, window_num)
                session_results.append(result)
                
                if result["status"] == "completed":
                    total_pnl += result.get("pnl", 0)
                    total_fills += result.get("fills", 0)
                    
                    print(f"\n[WINDOW {window_num}] Completed:", flush=True)
                    print(f"  PnL: ${result['pnl']:+.2f}", flush=True)
                    print(f"  Fills: {result['fills']}", flush=True)
                    print(f"  Trade PnL: {result['trade_pnl_cents']:+.1f}c", flush=True)
                    print(f"  Session Total: ${total_pnl:+.2f}, {total_fills} fills", flush=True)
            
            except Exception as e:
                print(f"[WINDOW {window_num}] Error: {e}", flush=True)
            
            # Wait for next window
            print(f"\n[WAITING] Next window... (checking every 5s)", flush=True)
            
            for i in range(60):  # Max 5 min wait
                time.sleep(5)
                
                try:
                    market = resolver.resolve_market()
                    if market and market.end_time > int(time.time()) + 60:
                        print(f"[NEW WINDOW] Found: {market.question}", flush=True)
                        break
                except:
                    pass
                
                if i % 6 == 0:
                    print(f"[WAITING] {i*5}s...", flush=True)
    
    except KeyboardInterrupt:
        print("\n\n[STOPPED] User interrupt", flush=True)
    
    finally:
        # Session summary
        print("\n" + "=" * 60, flush=True)
        print("  SESSION SUMMARY", flush=True)
        print("=" * 60, flush=True)
        print(f"  Windows: {window_num}", flush=True)
        print(f"  Total Fills: {total_fills}", flush=True)
        print(f"  Total PnL: ${total_pnl:+.2f}", flush=True)
        
        if total_fills > 0:
            est_rebates = total_fills * 0.005  # ~0.5c per fill
            print(f"  Est. Rebates: ${est_rebates:.2f}", flush=True)
            print(f"  NET (PnL + Rebates): ${total_pnl + est_rebates:+.2f}", flush=True)
        
        print("=" * 60, flush=True)


if __name__ == "__main__":
    main()

