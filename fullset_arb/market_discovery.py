"""
Market Discovery Module

Discovers binary markets on Polymarket via the Gamma API.
Filters for active, liquid markets suitable for full-set arbitrage.
"""

import logging
import time
import json
from typing import List, Dict, Optional, Tuple
from dataclasses import dataclass
import requests

from .config import ArbConfig, load_config

logger = logging.getLogger(__name__)


@dataclass
class BinaryMarket:
    """Represents a binary market with YES/NO tokens."""
    
    # Market identifiers
    condition_id: str
    question: str
    slug: str
    
    # Token IDs
    yes_token_id: str
    no_token_id: str
    
    # Market metadata
    volume_24h: float
    liquidity: float
    end_date: Optional[str]
    active: bool
    closed: bool
    
    # Optional: event info
    event_slug: Optional[str] = None
    event_title: Optional[str] = None
    
    @property
    def market_id(self) -> str:
        """Unique market identifier."""
        return self.condition_id
    
    def __repr__(self):
        return f"BinaryMarket({self.slug}, vol={self.volume_24h:.0f}, liq={self.liquidity:.0f})"


class MarketDiscovery:
    """
    Discovers and filters binary markets from Polymarket.
    
    Uses the Gamma API to fetch market metadata and filter for:
    - Active markets (not closed)
    - Binary markets (exactly 2 outcomes)
    - Sufficient volume and liquidity
    """
    
    def __init__(self, config: ArbConfig = None):
        self.config = config or load_config()
        self.session = requests.Session()
        self.session.headers.update({
            "User-Agent": "PolymarketArbBot/1.0",
            "Accept": "application/json"
        })
        
        # Cache of discovered markets
        self._market_cache: Dict[str, BinaryMarket] = {}
        self._cache_timestamp: float = 0
        self._cache_ttl: float = 60.0  # 1 minute cache
    
    def _get(self, url: str, params: Optional[dict] = None, timeout: float = 10.0) -> dict:
        """Make GET request with error handling."""
        try:
            response = self.session.get(url, params=params, timeout=timeout)
            response.raise_for_status()
            return response.json()
        except requests.RequestException as e:
            logger.error(f"API request failed: {e}")
            raise
    
    def _parse_market(self, market_data: dict, event_data: dict = None) -> Optional[BinaryMarket]:
        """Parse market JSON into BinaryMarket object."""
        try:
            # Get token IDs - Polymarket uses clobTokenIds [YES, NO]
            # NOTE: clobTokenIds can be a JSON string or a list
            token_ids_raw = market_data.get("clobTokenIds") or market_data.get("clob_token_ids") or []
            
            # Parse if it's a JSON string
            if isinstance(token_ids_raw, str):
                try:
                    token_ids = json.loads(token_ids_raw)
                except json.JSONDecodeError:
                    token_ids = []
            else:
                token_ids = token_ids_raw
            
            if not isinstance(token_ids, list) or len(token_ids) != 2:
                # Not a binary market
                return None
            
            yes_token_id = str(token_ids[0])
            no_token_id = str(token_ids[1])
            
            # Parse volume and liquidity
            volume_24h = float(market_data.get("volume24hr", 0) or 0)
            liquidity = float(market_data.get("liquidity", 0) or 0)
            
            # Check if active and not closed
            active = market_data.get("active", True)
            closed = market_data.get("closed", False)
            
            # Skip if order book is disabled
            if not market_data.get("enableOrderBook", True):
                return None
            
            return BinaryMarket(
                condition_id=market_data.get("conditionId", market_data.get("condition_id", "")),
                question=market_data.get("question", ""),
                slug=market_data.get("slug", ""),
                yes_token_id=yes_token_id,
                no_token_id=no_token_id,
                volume_24h=volume_24h,
                liquidity=liquidity,
                end_date=market_data.get("endDate") or market_data.get("end_date_iso"),
                active=active,
                closed=closed,
                event_slug=event_data.get("slug") if event_data else None,
                event_title=event_data.get("title") if event_data else None,
            )
        except Exception as e:
            logger.warning(f"Failed to parse market: {e}")
            return None
    
    def fetch_markets_from_events(self, limit: int = 100, offset: int = 0) -> List[BinaryMarket]:
        """
        Fetch markets by iterating through events.
        Events contain their markets, so this is efficient.
        """
        url = f"{self.config.gamma_api_url}/events"
        params = {
            "limit": limit,
            "offset": offset,
            "order": "volume24hr",  # Order by volume
            "ascending": "false",
            "closed": "false",
            "active": "true",
        }
        
        events = self._get(url, params=params)
        markets = []
        
        for event in events:
            event_markets = event.get("markets") or []
            for market_data in event_markets:
                market = self._parse_market(market_data, event)
                if market:
                    markets.append(market)
        
        return markets
    
    def fetch_markets_direct(self, limit: int = 100, offset: int = 0) -> List[BinaryMarket]:
        """
        Fetch markets directly from /markets endpoint.
        Alternative to event-based discovery.
        """
        url = f"{self.config.gamma_api_url}/markets"
        params = {
            "limit": limit,
            "offset": offset,
            "order": "volume24hr",
            "ascending": "false",
            "closed": "false",
            "active": "true",
        }
        
        market_list = self._get(url, params=params)
        markets = []
        
        for market_data in market_list:
            market = self._parse_market(market_data)
            if market:
                markets.append(market)
        
        return markets
    
    def discover_all(self, max_markets: int = None) -> List[BinaryMarket]:
        """
        Discover all suitable binary markets.
        
        Filters for:
        - Active and not closed
        - Minimum volume and liquidity
        - Binary markets only
        
        Returns markets sorted by volume (highest first).
        """
        max_markets = max_markets or self.config.max_markets_to_scan
        all_markets = []
        offset = 0
        batch_size = 100
        
        while len(all_markets) < max_markets:
            try:
                batch = self.fetch_markets_from_events(limit=batch_size, offset=offset)
                if not batch:
                    break
                
                # Filter by volume and liquidity
                for market in batch:
                    if market.closed or not market.active:
                        continue
                    
                    if market.volume_24h < self.config.min_volume_24h:
                        continue
                    
                    if market.liquidity < self.config.min_liquidity:
                        continue
                    
                    all_markets.append(market)
                    
                    if len(all_markets) >= max_markets:
                        break
                
                offset += batch_size
                
                # Rate limiting
                time.sleep(0.1)
                
            except Exception as e:
                logger.error(f"Error fetching markets at offset {offset}: {e}")
                break
        
        # Sort by volume (highest first)
        all_markets.sort(key=lambda m: m.volume_24h, reverse=True)
        
        # Update cache
        self._market_cache = {m.market_id: m for m in all_markets}
        self._cache_timestamp = time.time()
        
        logger.info(f"Discovered {len(all_markets)} suitable binary markets")
        return all_markets
    
    def get_cached_markets(self) -> List[BinaryMarket]:
        """Get markets from cache, refresh if stale."""
        if time.time() - self._cache_timestamp > self._cache_ttl:
            return self.discover_all()
        return list(self._market_cache.values())
    
    def get_market_by_id(self, market_id: str) -> Optional[BinaryMarket]:
        """Get a specific market by condition ID."""
        if market_id in self._market_cache:
            return self._market_cache[market_id]
        
        # Try to fetch directly
        try:
            url = f"{self.config.gamma_api_url}/markets/{market_id}"
            market_data = self._get(url)
            return self._parse_market(market_data)
        except Exception as e:
            logger.warning(f"Failed to fetch market {market_id}: {e}")
            return None


def main():
    """Test market discovery."""
    logging.basicConfig(level=logging.INFO)
    
    discovery = MarketDiscovery()
    markets = discovery.discover_all(max_markets=50)
    
    print(f"\n=== Discovered {len(markets)} Binary Markets ===\n")
    
    for i, market in enumerate(markets[:20]):
        print(f"{i+1:3}. {market.question[:60]}...")
        print(f"     Volume: ${market.volume_24h:,.0f} | Liquidity: ${market.liquidity:,.0f}")
        print(f"     YES: {market.yes_token_id[:20]}...")
        print(f"     NO:  {market.no_token_id[:20]}...")
        print()


if __name__ == "__main__":
    main()

