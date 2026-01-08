"""
Fetch live odds from multiple platforms.

Uses The Odds API (https://the-odds-api.com/) which aggregates odds from:
- DraftKings, FanDuel, BetMGM, Caesars, PointsBet, etc.

Free tier: 500 requests/month
"""
import requests
import json
from dataclasses import dataclass
from typing import List, Dict, Optional
from datetime import datetime

from .odds_math import Odds


@dataclass
class GameOdds:
    """Complete odds for a single game."""
    sport: str
    event_id: str
    home_team: str
    away_team: str
    commence_time: str
    
    # Odds by outcome by platform
    # {platform: {outcome: Odds}}
    odds_by_platform: Dict[str, Dict[str, Odds]]


class OddsAPIFetcher:
    """
    Fetch odds from The Odds API.
    
    Get free API key at: https://the-odds-api.com/
    """
    
    BASE_URL = "https://api.the-odds-api.com/v4"
    
    # Sport keys for the API
    SPORT_KEYS = {
        "NFL": "americanfootball_nfl",
        "NBA": "basketball_nba",
        "MLB": "baseball_mlb",
        "NHL": "icehockey_nhl",
        "UFC": "mma_mixed_martial_arts",
        "Soccer_EPL": "soccer_epl",
        "Soccer_UCL": "soccer_uefa_champions_league",
        "NCAA_Football": "americanfootball_ncaaf",
        "NCAA_Basketball": "basketball_ncaab"
    }
    
    def __init__(self, api_key: str):
        self.api_key = api_key
        self.requests_remaining = None
        self.requests_used = None
    
    def get_sports(self) -> List[Dict]:
        """Get list of available sports."""
        url = f"{self.BASE_URL}/sports"
        params = {"apiKey": self.api_key}
        
        resp = requests.get(url, params=params)
        resp.raise_for_status()
        
        return resp.json()
    
    def get_odds(
        self,
        sport: str,
        regions: str = "us",
        markets: str = "h2h",  # head-to-head (moneyline)
        odds_format: str = "american"
    ) -> List[GameOdds]:
        """
        Fetch odds for a sport.
        
        Args:
            sport: Sport key (e.g., "NFL", "NBA") or API key
            regions: Regions to get odds from ("us", "uk", "eu", "au")
            markets: Market types ("h2h", "spreads", "totals")
            odds_format: "american" or "decimal"
        """
        sport_key = self.SPORT_KEYS.get(sport, sport)
        
        url = f"{self.BASE_URL}/sports/{sport_key}/odds"
        params = {
            "apiKey": self.api_key,
            "regions": regions,
            "markets": markets,
            "oddsFormat": odds_format
        }
        
        resp = requests.get(url, params=params)
        
        # Track API usage
        self.requests_remaining = resp.headers.get("x-requests-remaining")
        self.requests_used = resp.headers.get("x-requests-used")
        
        resp.raise_for_status()
        data = resp.json()
        
        games = []
        for event in data:
            game = self._parse_event(event, odds_format)
            if game:
                games.append(game)
        
        return games
    
    def _parse_event(self, event: Dict, odds_format: str) -> Optional[GameOdds]:
        """Parse a single event from API response."""
        try:
            odds_by_platform = {}
            
            for bookmaker in event.get("bookmakers", []):
                platform = bookmaker["key"]
                platform_odds = {}
                
                for market in bookmaker.get("markets", []):
                    if market["key"] == "h2h":  # Moneyline
                        for outcome in market.get("outcomes", []):
                            name = outcome["name"]
                            price = outcome["price"]
                            
                            if odds_format == "american":
                                odds = Odds.from_american(platform, name, price)
                            else:
                                odds = Odds.from_decimal(platform, name, price)
                            
                            platform_odds[name] = odds
                
                if platform_odds:
                    odds_by_platform[platform] = platform_odds
            
            return GameOdds(
                sport=event.get("sport_key", ""),
                event_id=event.get("id", ""),
                home_team=event.get("home_team", ""),
                away_team=event.get("away_team", ""),
                commence_time=event.get("commence_time", ""),
                odds_by_platform=odds_by_platform
            )
        except Exception as e:
            print(f"Error parsing event: {e}")
            return None


class PolymarketFetcher:
    """Fetch sports markets from Polymarket."""
    
    BASE_URL = "https://gamma-api.polymarket.com"
    
    def __init__(self):
        pass
    
    def search_sports_markets(self, query: str = "Super Bowl") -> List[Dict]:
        """Search for sports-related markets."""
        url = f"{self.BASE_URL}/markets"
        params = {
            "closed": "false",
            "limit": 100
        }
        
        try:
            resp = requests.get(url, params=params, timeout=10)
            resp.raise_for_status()
            markets = resp.json()
            
            # Filter for sports-related
            sports_keywords = [
                "NFL", "NBA", "MLB", "NHL", "UFC", "Super Bowl",
                "World Series", "Stanley Cup", "Finals", "Championship",
                "win", "beat", "vs"
            ]
            
            sports_markets = []
            for m in markets:
                question = m.get("question", "").lower()
                if any(kw.lower() in question for kw in sports_keywords):
                    sports_markets.append(m)
            
            return sports_markets
        except Exception as e:
            print(f"Error fetching Polymarket: {e}")
            return []
    
    def get_market_prices(self, condition_id: str) -> Dict:
        """Get current prices for a market."""
        url = f"{self.BASE_URL}/markets/{condition_id}"
        
        try:
            resp = requests.get(url, timeout=10)
            resp.raise_for_status()
            return resp.json()
        except Exception as e:
            print(f"Error fetching market prices: {e}")
            return {}


def demo_without_api():
    """Demo the math without API calls."""
    print("=" * 70)
    print("LIVE SPORTS ARBITRAGE - DEMO MODE")
    print("=" * 70)
    print("\nTo use live data, get a free API key from:")
    print("  https://the-odds-api.com/")
    print("\nThen run:")
    print("  python -m sports_arb.scanner --api-key YOUR_KEY")
    print()
    
    # Simulate what real data looks like
    print("SIMULATED EXAMPLE: NFL Game")
    print("-" * 50)
    print("\nSportsbook Odds for Chiefs vs Bills:")
    print()
    print("  DraftKings:")
    print("    Chiefs: -150 (implied: 60.0%)")
    print("    Bills:  +130 (implied: 43.5%)")
    print("    Total implied: 103.5% (house edge: 3.5%)")
    print()
    print("  FanDuel:")
    print("    Chiefs: -145 (implied: 59.2%)")
    print("    Bills:  +135 (implied: 42.6%)")
    print("    Total implied: 101.8% (house edge: 1.8%)")
    print()
    print("  Polymarket:")
    print("    Chiefs: 58c (implied: 58.0%)")
    print("    Bills:  43c (implied: 43.0%)")
    print("    Total implied: 101.0% (house edge: 1.0%)")
    print()
    
    print("*** CROSS-PLATFORM ANALYSIS ***")
    print()
    print("Best Chiefs odds: Polymarket 58c (decimal: 1.724)")
    print("Best Bills odds:  FanDuel +135 (decimal: 2.350)")
    print()
    print("Combined implied: 58.0% + 42.6% = 100.6%")
    print("Result: NO ARBITRAGE (need < 100%)")
    print()
    
    print("=" * 70)
    print("WHEN DOES ARBITRAGE HAPPEN?")
    print("=" * 70)
    print("""
Arbitrage opportunities appear when:

1. LIVE EVENTS (most common)
   - During a game, odds shift rapidly
   - One book might be slow to update
   - Window: seconds to minutes

2. BREAKING NEWS
   - Injury announcements
   - Lineup changes
   - Weather updates
   - One book reacts before others

3. LINE MOVEMENT
   - Heavy betting on one side
   - Books adjust at different speeds
   
4. PROMOTION ODDS
   - "Odds boost" promotions
   - New customer specials
   - These can create real arb

TYPICAL EDGE: 1-3% when found
FREQUENCY: Rare for pre-game, more common live
""")


if __name__ == "__main__":
    demo_without_api()

