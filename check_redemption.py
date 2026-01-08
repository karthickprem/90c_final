"""
Check positions and their redemption status
"""

import json
import requests

with open("pm_api_config.json") as f:
    config = json.load(f)

proxy = config["proxy_address"]

print("=" * 70)
print("CHECKING POSITION REDEMPTION STATUS")
print("=" * 70)

# Get positions with full details
r = requests.get(f"https://data-api.polymarket.com/positions?user={proxy}", timeout=10)
positions = r.json()

print(f"\nFound {len(positions)} positions:\n")

total_redeemable = 0
total_lost = 0

for i, pos in enumerate(positions):
    size = float(pos.get("size", 0))
    outcome = pos.get("outcome", "?")
    token_id = pos.get("asset", "")
    avg_price = float(pos.get("avgPrice", 0))
    cost = size * avg_price
    
    # Get detailed market info from condition_id
    condition_id = pos.get("conditionId", "")
    
    # Check if market is resolved and what the outcome was
    market = pos.get("market", {})
    resolved = market.get("resolved", False)
    winning_outcome = market.get("winningOutcome", None)
    question = market.get("question", "Unknown")
    
    print(f"[{i+1}] {outcome}: {size:.2f} shares @ avg {avg_price*100:.0f}c")
    print(f"    Cost: ${cost:.2f}")
    print(f"    Token: {token_id[:40]}...")
    print(f"    Market: {question[:60] if question else 'Unknown'}...")
    print(f"    Resolved: {resolved}")
    
    if resolved:
        if winning_outcome:
            print(f"    Winner: {winning_outcome}")
            if outcome.lower() == str(winning_outcome).lower():
                payout = size * 1.0
                profit = payout - cost
                total_redeemable += payout
                print(f"    -> YOU WON! Redeemable: ${payout:.2f} (profit: ${profit:+.2f})")
            else:
                total_lost += cost
                print(f"    -> You lost. Value: $0.00")
        else:
            print(f"    -> Resolved but no winner info")
    else:
        # Check if it's an expired market (orderbook doesn't exist)
        print(f"    -> Not resolved yet (or data stale)")
    
    print()

print("=" * 70)
print("SUMMARY")
print("=" * 70)
print(f"Potential redeemable: ${total_redeemable:.2f}")
print(f"Losses: ${total_lost:.2f}")
print()
print("TO REDEEM:")
print("1. Go to polymarket.com")
print("2. Click on your Portfolio")
print("3. Look for 'Redeem' or 'Claim' buttons on resolved markets")
print("4. Click to convert winning shares to USDC")
print()
print("For BTC 15-min markets that expired:")
print("- They should auto-settle after resolution")
print("- Check 'Activity' tab for settlement status")
print("=" * 70)

