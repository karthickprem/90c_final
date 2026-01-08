"""Configuration for sports arbitrage."""
from dataclasses import dataclass, field
from typing import List


@dataclass
class SportsConfig:
    """Sports arbitrage configuration."""
    
    # Minimum edge to consider (as percentage)
    min_edge_pct: float = 1.0
    
    # Maximum stake per bet
    max_stake: float = 100.0
    
    # Platforms to monitor
    platforms: List[str] = field(default_factory=lambda: [
        "polymarket",
        "draftkings", 
        "fanduel",
        "betmgm",
        "pinnacle"
    ])
    
    # Sports to focus on
    sports: List[str] = field(default_factory=lambda: [
        "NFL",
        "NBA", 
        "UFC",
        "MLB",
        "NHL",
        "Soccer"
    ])
    
    # Refresh interval for odds (seconds)
    refresh_interval: float = 5.0


# API endpoints (you'll need to get API keys for these)
ENDPOINTS = {
    "polymarket": {
        "base": "https://gamma-api.polymarket.com",
        "markets": "/markets",
        "events": "/events"
    },
    "odds_api": {
        # The Odds API - aggregates odds from multiple sportsbooks
        # https://the-odds-api.com/
        "base": "https://api.the-odds-api.com/v4",
        "sports": "/sports",
        "odds": "/sports/{sport}/odds"
    }
}

