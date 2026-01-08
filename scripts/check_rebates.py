#!/usr/bin/env python3
"""
Check Actual Maker Rebates
==========================
Computes real rebate_per_share from wallet activity.

Run this AFTER a trading day to see actual rebates credited.

Usage:
    python scripts/check_rebates.py

Outputs:
    - executed_maker_shares (from fill tracker / trades)
    - rebates_credited_usdc (from wallet rewards)
    - rebate_per_share = rebates / shares
"""

import os
import sys
import json
from datetime import datetime, timedelta
from pathlib import Path

# Add parent to path
sys.path.insert(0, str(Path(__file__).parent.parent))

from mm_bot.config import Config
from mm_bot.clob import ClobWrapper


def main():
    print("=" * 60)
    print("MAKER REBATE ANALYSIS")
    print("=" * 60)
    print()
    
    # Load config
    config = Config.from_env("pm_api_config.json")
    clob = ClobWrapper(config)
    
    # Get recent trades (last 24h)
    print("Fetching recent trades...")
    try:
        trades = clob.client.get_trades()
        print(f"Found {len(trades) if trades else 0} trades")
    except Exception as e:
        print(f"Error fetching trades: {e}")
        trades = []
    
    # Calculate maker shares
    maker_shares = 0
    taker_shares = 0
    maker_volume_usd = 0
    taker_volume_usd = 0
    
    if trades:
        for trade in trades:
            size = float(trade.get("size", 0))
            price = float(trade.get("price", 0))
            is_maker = trade.get("maker", False) or trade.get("is_maker", False)
            
            if is_maker:
                maker_shares += size
                maker_volume_usd += size * price
            else:
                taker_shares += size
                taker_volume_usd += size * price
    
    print()
    print("TRADE SUMMARY (recent):")
    print(f"  Maker fills: {maker_shares:.2f} shares (${maker_volume_usd:.2f})")
    print(f"  Taker fills: {taker_shares:.2f} shares (${taker_volume_usd:.2f})")
    print(f"  Total: {maker_shares + taker_shares:.2f} shares")
    
    # Get balance info to find rebates
    print()
    print("BALANCE INFO:")
    try:
        balance = clob.get_balance()
        print(f"  USDC Balance: ${balance:.4f}")
    except Exception as e:
        print(f"  Error fetching balance: {e}")
    
    # Note about rebates
    print()
    print("=" * 60)
    print("HOW TO CHECK REBATES:")
    print("=" * 60)
    print("""
1. Rebates are paid daily at ~midnight UTC
2. Check your Polymarket Portfolio -> History
3. Look for "Maker Rebate" or "Reward" entries
4. Record: rebates_credited_usdc

Then compute:
    rebate_per_share = rebates_credited_usdc / maker_shares

Example:
    If maker_shares = 100 and rebates = $0.50
    rebate_per_share = $0.50 / 100 = $0.005 = 0.5c per share

IMPORTANT:
- This is MUCH less than the $1.56/100 shares taker fee!
- The taker fee is split among ALL makers proportionally
- Your actual rebate depends on your share of total market volume
""")
    
    # Calculate what we need to measure
    print("=" * 60)
    print("WHAT TO TRACK TOMORROW:")
    print("=" * 60)
    print(f"""
After running the bot today, record:
1. executed_maker_shares = {maker_shares:.2f} (from this script)
2. Tomorrow, check wallet for rebate credit

Then update config:
    ACTUAL_REBATE_PER_SHARE = rebates_credited / {maker_shares:.2f}
    
If actual rebate is:
    < 1c/share: Don't rely on rebates, break-even on trade P&L
    1-2c/share: Can afford 1c loss per trade
    > 2c/share: Can afford 2c loss per trade (unlikely unless you're dominant maker)
""")
    
    # Save to file for tracking
    output = {
        "timestamp": datetime.now().isoformat(),
        "maker_shares": maker_shares,
        "taker_shares": taker_shares,
        "maker_volume_usd": maker_volume_usd,
        "taker_volume_usd": taker_volume_usd,
        "notes": "Check wallet tomorrow for rebate credit"
    }
    
    output_path = Path("mm_out_v5_rebate_tracking.json")
    with open(output_path, "w") as f:
        json.dump(output, f, indent=2)
    
    print(f"\nSaved to: {output_path}")


if __name__ == "__main__":
    main()

