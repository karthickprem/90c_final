"""
FEE ANALYSIS: Find the minimum edge needed to overcome fees
"""
import sys

sys.stdout.reconfigure(encoding='utf-8')


def polymarket_fee(price_cents: int, size_dollars: float) -> float:
    """Calculate Polymarket taker fee."""
    if price_cents <= 0 or price_cents >= 100:
        return 0
    p = price_cents / 100.0
    shares = size_dollars / (price_cents / 100.0)
    fee_per_share = 0.25 * (p * (1 - p)) ** 2
    return shares * fee_per_share


def analyze_fullset_fees():
    """Analyze fees for full-set trades at different combined costs."""
    print("=" * 70)
    print("FULL-SET FEE ANALYSIS")
    print("=" * 70)
    print("\nFor $10 per leg ($20 total position):")
    print()
    print(f"{'Combined':>10} {'Edge':>6} {'UP_fee':>8} {'DN_fee':>8} {'TotalFee':>10} {'Net Edge':>10} {'Profit?':>8}")
    print("-" * 70)
    
    for combined in range(100, 89, -1):
        # Assume roughly equal prices
        up_price = combined // 2
        down_price = combined - up_price
        
        edge = 100 - combined
        gross = edge / 100 * 10.0 * 2  # $10 per leg, 2 legs
        
        up_fee = polymarket_fee(up_price, 10.0)
        down_fee = polymarket_fee(down_price, 10.0)
        total_fee = up_fee + down_fee
        
        net = gross - total_fee
        profit = "YES" if net > 0 else "NO"
        
        print(f"{combined}c       {edge}c     ${up_fee:.3f}   ${down_fee:.3f}   ${total_fee:.3f}      ${net:.3f}      {profit}")
    
    print("\n" + "=" * 70)
    print("AT EXTREME PRICES (90/5, 85/10, etc.):")
    print("=" * 70)
    print()
    
    test_cases = [
        (95, 4),
        (90, 8),
        (90, 5),
        (85, 10),
        (80, 15),
        (75, 20),
        (70, 25),
    ]
    
    print(f"{'UP':>5} {'DN':>5} {'Combined':>10} {'Edge':>6} {'TotalFee':>10} {'Net':>10}")
    print("-" * 55)
    
    for up, down in test_cases:
        combined = up + down
        edge = 100 - combined
        gross = edge / 100 * 10.0 * 2
        
        up_fee = polymarket_fee(up, 10.0)
        down_fee = polymarket_fee(down, 10.0)
        total_fee = up_fee + down_fee
        
        net = gross - total_fee
        print(f"{up}c   {down}c   {combined}c         {edge}c    ${total_fee:.3f}      ${net:.3f}")
    
    print("\n" + "=" * 70)
    print("KEY INSIGHT")
    print("=" * 70)
    print("""
    The Polymarket fee is HIGHEST near 50c and LOWEST at extremes.
    
    Fee formula: fee = shares * 0.25 * (p * (1-p))^2
    
    At 50c: fee per share = 0.25 * (0.5 * 0.5)^2 = 0.25 * 0.0625 = 0.0156 = 1.56%
    At 90c: fee per share = 0.25 * (0.9 * 0.1)^2 = 0.25 * 0.0081 = 0.002 = 0.2%
    At 10c: fee per share = 0.25 * (0.1 * 0.9)^2 = 0.25 * 0.0081 = 0.002 = 0.2%
    
    This means:
    - Full-sets near 50/50 are EXPENSIVE (high fees)
    - Full-sets at extremes (90/5, 85/10) are CHEAP (low fees)
    
    BUT: Full-sets at extremes (90+5=95c) have SMALL edge (5c)
    AND: Full-sets with BIG edge (like 70+20=90c) are RARE
    """)


def analyze_directional_fees():
    """Analyze fees for directional trades."""
    print("\n" + "=" * 70)
    print("DIRECTIONAL FEE ANALYSIS")
    print("=" * 70)
    print("\nFor $10 position, entry only (hold to settlement = no exit fee):")
    print()
    print(f"{'Entry':>8} {'Fee':>8} {'Profit if Win':>14} {'Loss if Lose':>14} {'Break-even WR':>14}")
    print("-" * 65)
    
    for entry in [88, 90, 92, 94, 95, 96]:
        fee = polymarket_fee(entry, 10.0)
        
        # If win: get 100c, paid entry+fee
        profit_if_win = (100 - entry) / 100 * 10.0 - fee
        
        # If lose: lose entry + fee
        loss_if_lose = entry / 100 * 10.0 + fee
        
        # Break-even: win_rate * profit = (1-win_rate) * loss
        # win_rate = loss / (profit + loss)
        breakeven = loss_if_lose / (profit_if_win + loss_if_lose) * 100
        
        print(f"{entry}c      ${fee:.3f}    ${profit_if_win:.3f}         -${loss_if_lose:.3f}          {breakeven:.1f}%")
    
    print("""
    At 92c entry with fee ~$0.02:
    - Win: profit = (100-92)/100 * $10 - $0.02 = $0.78
    - Lose: loss = 92/100 * $10 + $0.02 = $9.22
    - Break-even win rate: 92.2 / (0.78 + 9.22) = 92.2%
    
    Our filtered strategy got 95.3% at 92c with score <= -1
    That's ABOVE break-even -> PROFITABLE!
    """)


def main():
    analyze_fullset_fees()
    analyze_directional_fees()
    
    print("\n" + "=" * 70)
    print("CONCLUSION")
    print("=" * 70)
    print("""
    The earlier combined strategy was LOSING because:
    
    1. Full-set at 98c = 2c edge, but fees ~$0.55 = LOSS
    2. Full-set only works when combined cost < ~95c
    3. Directional at 92c works if win rate > 92%
    
    REVISED STRATEGY:
    - Full-set: ONLY if combined cost <= 95c (edge >= 5c)
    - Directional: Entry >= 92c with score <= -1
    """)


if __name__ == "__main__":
    main()

