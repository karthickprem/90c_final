"""
Core arbitrage math for sports betting.

Key concept: If implied probabilities sum to < 100%, there's arbitrage.
"""
from dataclasses import dataclass
from typing import List, Tuple, Optional


@dataclass
class Odds:
    """Represents odds from a sportsbook."""
    platform: str
    outcome: str  # e.g., "Team A wins", "Over 45.5"
    american: int  # American odds: +150, -200, etc.
    decimal: float  # Decimal odds: 2.50, 1.50, etc.
    implied_prob: float  # 0.0 to 1.0
    
    @classmethod
    def from_american(cls, platform: str, outcome: str, american: int) -> "Odds":
        """Create from American odds."""
        if american > 0:
            decimal = (american / 100) + 1
            implied = 100 / (american + 100)
        else:
            decimal = (100 / abs(american)) + 1
            implied = abs(american) / (abs(american) + 100)
        
        return cls(
            platform=platform,
            outcome=outcome,
            american=american,
            decimal=decimal,
            implied_prob=implied
        )
    
    @classmethod
    def from_decimal(cls, platform: str, outcome: str, decimal: float) -> "Odds":
        """Create from decimal odds."""
        if decimal >= 2.0:
            american = int((decimal - 1) * 100)
        else:
            american = int(-100 / (decimal - 1))
        
        implied = 1 / decimal
        
        return cls(
            platform=platform,
            outcome=outcome,
            american=american,
            decimal=decimal,
            implied_prob=implied
        )
    
    @classmethod
    def from_probability(cls, platform: str, outcome: str, prob: float) -> "Odds":
        """Create from implied probability (like Polymarket prices)."""
        if prob <= 0 or prob >= 1:
            raise ValueError(f"Probability must be between 0 and 1, got {prob}")
        
        decimal = 1 / prob
        
        if decimal >= 2.0:
            american = int((decimal - 1) * 100)
        else:
            american = int(-100 / (decimal - 1))
        
        return cls(
            platform=platform,
            outcome=outcome,
            american=american,
            decimal=decimal,
            implied_prob=prob
        )


@dataclass
class ArbitrageOpportunity:
    """Represents an arbitrage opportunity."""
    event: str
    outcomes: List[Odds]  # Best odds for each outcome
    total_implied: float  # Sum of implied probabilities
    edge_pct: float  # Guaranteed profit percentage
    stakes: List[Tuple[str, str, float]]  # (platform, outcome, stake)
    expected_profit: float


def find_arbitrage(
    event: str,
    all_odds: List[List[Odds]],  # Odds for each outcome from all platforms
    total_stake: float = 100.0
) -> Optional[ArbitrageOpportunity]:
    """
    Find arbitrage opportunity from odds across platforms.
    
    Args:
        event: Event name
        all_odds: For each outcome, list of odds from different platforms
        total_stake: Total amount to bet
    
    Returns:
        ArbitrageOpportunity if edge exists, None otherwise
    """
    # Find best odds for each outcome (highest decimal = best payout)
    best_odds = []
    for outcome_odds in all_odds:
        if not outcome_odds:
            return None
        best = max(outcome_odds, key=lambda x: x.decimal)
        best_odds.append(best)
    
    # Calculate total implied probability
    total_implied = sum(o.implied_prob for o in best_odds)
    
    # If total < 1.0, there's arbitrage!
    if total_implied >= 1.0:
        return None
    
    edge_pct = (1 - total_implied) * 100
    
    # Calculate optimal stakes (Kelly-like distribution)
    stakes = []
    for odds in best_odds:
        # Stake proportional to inverse of decimal odds
        stake = (total_stake / total_implied) * odds.implied_prob
        stakes.append((odds.platform, odds.outcome, stake))
    
    # Calculate expected profit
    # No matter which outcome wins, you get back the same amount
    # That amount is: stake_i * decimal_i for any i
    expected_return = stakes[0][2] * best_odds[0].decimal
    expected_profit = expected_return - total_stake
    
    return ArbitrageOpportunity(
        event=event,
        outcomes=best_odds,
        total_implied=total_implied,
        edge_pct=edge_pct,
        stakes=stakes,
        expected_profit=expected_profit
    )


def calculate_stakes(
    odds_list: List[Odds],
    total_stake: float = 100.0,
    target_profit: Optional[float] = None
) -> List[Tuple[str, str, float]]:
    """
    Calculate optimal stake distribution for arbitrage.
    
    If target_profit specified, calculates stakes to achieve that profit.
    Otherwise, distributes for equal return regardless of outcome.
    """
    total_implied = sum(o.implied_prob for o in odds_list)
    
    if total_implied >= 1.0:
        raise ValueError("No arbitrage opportunity - implied probabilities >= 100%")
    
    stakes = []
    for odds in odds_list:
        stake = (total_stake / total_implied) * odds.implied_prob
        stakes.append((odds.platform, odds.outcome, stake))
    
    return stakes


# Example usage and demonstration
def demo():
    """Demonstrate arbitrage calculation."""
    print("=" * 60)
    print("SPORTS ARBITRAGE CALCULATOR DEMO")
    print("=" * 60)
    
    # Example: NFL game
    # Team A: DraftKings +150, Polymarket 40c (40% = +150 equivalent)
    # Team B: FanDuel -130, Polymarket 55c
    
    print("\nExample: NFL Game - Team A vs Team B")
    print("-" * 40)
    
    # Odds for Team A from different platforms
    team_a_odds = [
        Odds.from_american("DraftKings", "Team A wins", 150),
        Odds.from_probability("Polymarket", "Team A wins", 0.40),
    ]
    
    # Odds for Team B from different platforms  
    team_b_odds = [
        Odds.from_american("FanDuel", "Team B wins", -130),
        Odds.from_probability("Polymarket", "Team B wins", 0.55),
    ]
    
    print("\nTeam A odds:")
    for o in team_a_odds:
        print(f"  {o.platform}: {o.american:+d} (decimal: {o.decimal:.3f}, implied: {o.implied_prob*100:.1f}%)")
    
    print("\nTeam B odds:")
    for o in team_b_odds:
        print(f"  {o.platform}: {o.american:+d} (decimal: {o.decimal:.3f}, implied: {o.implied_prob*100:.1f}%)")
    
    # Find arbitrage
    arb = find_arbitrage(
        "NFL: Team A vs Team B",
        [team_a_odds, team_b_odds],
        total_stake=100.0
    )
    
    if arb:
        print(f"\n*** ARBITRAGE FOUND! ***")
        print(f"Total implied: {arb.total_implied*100:.2f}%")
        print(f"Edge: {arb.edge_pct:.2f}%")
        print(f"Expected profit: ${arb.expected_profit:.2f}")
        print(f"\nBetting instructions:")
        for platform, outcome, stake in arb.stakes:
            print(f"  Bet ${stake:.2f} on '{outcome}' at {platform}")
    else:
        print("\nNo arbitrage opportunity found.")
    
    # Show a clear arbitrage example
    print("\n" + "=" * 60)
    print("GUARANTEED ARBITRAGE EXAMPLE")
    print("=" * 60)
    
    # Construct a clear arb: if you can get Team A at +200 and Team B at +150
    clear_a = [Odds.from_american("Platform1", "Team A", 200)]
    clear_b = [Odds.from_american("Platform2", "Team B", 150)]
    
    print("\nScenario: Two-outcome event")
    print(f"  Platform1 offers Team A at +200 (implied: 33.3%)")
    print(f"  Platform2 offers Team B at +150 (implied: 40.0%)")
    print(f"  Total implied: 73.3% < 100%")
    
    arb2 = find_arbitrage("Clear Example", [clear_a, clear_b], 100.0)
    if arb2:
        print(f"\n*** ARBITRAGE! ***")
        print(f"Edge: {arb2.edge_pct:.2f}%")
        print(f"Guaranteed profit on $100: ${arb2.expected_profit:.2f}")
        print(f"\nHow to bet:")
        for platform, outcome, stake in arb2.stakes:
            odds = next(o for o in arb2.outcomes if o.outcome == outcome)
            payout = stake * odds.decimal
            print(f"  ${stake:.2f} on {outcome} @ {platform} -> pays ${payout:.2f} if wins")


if __name__ == "__main__":
    demo()

