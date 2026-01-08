"""
Live sports arbitrage scanner.

Scans multiple platforms for arbitrage opportunities.
"""
import argparse
import time
from datetime import datetime
from typing import List, Optional

from .odds_math import Odds, ArbitrageOpportunity, find_arbitrage
from .odds_fetcher import OddsAPIFetcher, GameOdds


def scan_for_arbitrage(
    games: List[GameOdds],
    min_edge_pct: float = 0.5,
    stake: float = 100.0
) -> List[ArbitrageOpportunity]:
    """
    Scan list of games for arbitrage opportunities.
    """
    opportunities = []
    
    for game in games:
        # Get all unique outcomes
        all_outcomes = set()
        for platform_odds in game.odds_by_platform.values():
            all_outcomes.update(platform_odds.keys())
        
        if len(all_outcomes) < 2:
            continue
        
        # Build odds lists for each outcome
        outcome_odds = {}
        for outcome in all_outcomes:
            outcome_odds[outcome] = []
            for platform, p_odds in game.odds_by_platform.items():
                if outcome in p_odds:
                    outcome_odds[outcome].append(p_odds[outcome])
        
        # Find best odds for each outcome
        odds_lists = [outcome_odds[o] for o in sorted(all_outcomes)]
        
        arb = find_arbitrage(
            f"{game.away_team} @ {game.home_team}",
            odds_lists,
            stake
        )
        
        if arb and arb.edge_pct >= min_edge_pct:
            opportunities.append(arb)
    
    return opportunities


def print_opportunities(opps: List[ArbitrageOpportunity]):
    """Print arbitrage opportunities in a nice format."""
    if not opps:
        print("\nNo arbitrage opportunities found.")
        return
    
    print(f"\n{'='*70}")
    print(f"ARBITRAGE OPPORTUNITIES FOUND: {len(opps)}")
    print("="*70)
    
    for i, arb in enumerate(opps, 1):
        print(f"\n[{i}] {arb.event}")
        print(f"    Edge: {arb.edge_pct:.2f}%")
        print(f"    Guaranteed profit on $100: ${arb.expected_profit:.2f}")
        print(f"    Total implied probability: {arb.total_implied*100:.1f}%")
        print()
        print("    Betting instructions:")
        for platform, outcome, stake in arb.stakes:
            odds = next(o for o in arb.outcomes if o.outcome == outcome)
            print(f"      ${stake:.2f} on '{outcome}' at {platform} ({odds.american:+d})")


def run_scanner(api_key: str, sports: List[str], min_edge: float = 0.5):
    """Run the arbitrage scanner."""
    fetcher = OddsAPIFetcher(api_key)
    
    print("=" * 70)
    print("LIVE SPORTS ARBITRAGE SCANNER")
    print("=" * 70)
    print(f"\nScanning sports: {', '.join(sports)}")
    print(f"Minimum edge: {min_edge}%")
    print()
    
    all_games = []
    for sport in sports:
        print(f"Fetching {sport} odds...")
        try:
            games = fetcher.get_odds(sport)
            print(f"  Found {len(games)} games")
            all_games.extend(games)
        except Exception as e:
            print(f"  Error: {e}")
    
    print(f"\nAPI requests remaining: {fetcher.requests_remaining}")
    
    # Scan for arbitrage
    print("\nScanning for arbitrage...")
    opportunities = scan_for_arbitrage(all_games, min_edge)
    
    print_opportunities(opportunities)
    
    return opportunities


def run_demo():
    """Run demo without API key."""
    print("=" * 70)
    print("LIVE SPORTS ARBITRAGE SCANNER - DEMO")
    print("=" * 70)
    print("\nTo scan live odds, you need an API key from:")
    print("  https://the-odds-api.com/")
    print("  (Free tier: 500 requests/month)")
    print()
    print("Run with: python -m sports_arb.scanner --api-key YOUR_KEY")
    print()
    
    # Show sample opportunity
    print("-" * 70)
    print("SAMPLE ARBITRAGE OPPORTUNITY (simulated)")
    print("-" * 70)
    
    # Create sample odds that show arbitrage
    sample_a = [
        Odds.from_american("DraftKings", "Kansas City Chiefs", 180),
        Odds.from_american("FanDuel", "Kansas City Chiefs", 175),
    ]
    sample_b = [
        Odds.from_american("BetMGM", "Buffalo Bills", 120),
        Odds.from_american("Caesars", "Buffalo Bills", 115),
    ]
    
    arb = find_arbitrage("NFL: Chiefs @ Bills", [sample_a, sample_b], 100.0)
    
    if arb:
        print_opportunities([arb])
    else:
        print("\nNo arb in this example (realistic - arbs are rare!)")
    
    print()
    print("-" * 70)
    print("HOW TO FIND REAL ARBITRAGE:")
    print("-" * 70)
    print("""
1. GET API ACCESS:
   - The Odds API (https://the-odds-api.com/) - Free tier available
   - Or scrape directly from sportsbook sites
   
2. MONITOR LIVE GAMES:
   - Odds change rapidly during games
   - Slow-updating books create arb windows
   
3. USE AUTOMATION:
   - Manual scanning is too slow
   - Arb windows last seconds to minutes
   
4. HAVE ACCOUNTS READY:
   - DraftKings, FanDuel, BetMGM, Caesars, etc.
   - Funds deposited and ready to bet
   
5. WATCH FOR BOOSTS:
   - "Odds boost" promotions create arb
   - Often the best opportunities
""")


def main():
    parser = argparse.ArgumentParser(description="Live Sports Arbitrage Scanner")
    parser.add_argument("--api-key", type=str, help="The Odds API key")
    parser.add_argument(
        "--sports", 
        type=str, 
        default="NFL,NBA",
        help="Comma-separated list of sports to scan"
    )
    parser.add_argument(
        "--min-edge",
        type=float,
        default=0.5,
        help="Minimum edge percentage to report"
    )
    parser.add_argument(
        "--demo",
        action="store_true",
        help="Run in demo mode without API"
    )
    
    args = parser.parse_args()
    
    if args.demo or not args.api_key:
        run_demo()
    else:
        sports = [s.strip() for s in args.sports.split(",")]
        run_scanner(args.api_key, sports, args.min_edge)


if __name__ == "__main__":
    main()

