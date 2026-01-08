"""
Market Discovery for BTC 15-Minute Up/Down Markets

Discovers the active BTC 15-minute interval market on Polymarket.
These markets have YES/NO outcomes for "Bitcoin price up or down?"
over 15-minute windows.

Key features:
- Find current active window
- Get start/end timestamps
- Extract YES/NO token IDs
- Track upcoming windows
"""

import logging
import time
import json
import re
from typing import Optional, List, Dict, Tuple
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
import requests

from .config import ArbConfig, load_config

logger = logging.getLogger(__name__)


@dataclass
class BTC15mMarket:
    """
    Represents a BTC 15-minute Up/Down market window.
    """
    # Market identifiers
    market_id: str  # Polymarket condition ID
    event_id: str   # Parent event ID
    slug: str
    question: str
    
    # Token IDs for YES and NO
    yes_token_id: str
    no_token_id: str
    
    # Window timing
    window_id: str  # Human-readable window ID (e.g., "2024-01-15_14:00")
    start_ts: datetime
    end_ts: datetime
    
    # Market state
    active: bool
    closed: bool
    
    # Optional metadata
    volume_24h: float = 0.0
    liquidity: float = 0.0
    
    @property
    def duration_seconds(self) -> float:
        """Window duration in seconds."""
        return (self.end_ts - self.start_ts).total_seconds()
    
    @property
    def seconds_remaining(self) -> float:
        """Seconds until window closes."""
        now = datetime.now(timezone.utc)
        return max(0, (self.end_ts - now).total_seconds())
    
    @property
    def is_trading_allowed(self) -> bool:
        """Check if we should be trading (not too close to end)."""
        return self.seconds_remaining > 30  # Default cutoff
    
    def __repr__(self):
        return f"BTC15mMarket({self.window_id}, ends={self.end_ts.strftime('%H:%M:%S')})"


class BTC15mMarketDiscovery:
    """
    Discovers BTC 15-minute Up/Down markets from Polymarket.
    
    Strategy:
    1. Search for events matching "Bitcoin" + "15 minute" + "up or down"
    2. Find the currently active market (or next upcoming)
    3. Extract YES/NO token IDs
    4. Parse window start/end times
    """
    
    # Search patterns for BTC 15-min markets
    SEARCH_PATTERNS = [
        "bitcoin 15 minute",
        "btc 15 min",
        "bitcoin price 15 minute",
        "bitcoin up or down 15",
    ]
    
    def __init__(self, config: ArbConfig = None):
        self.config = config or load_config()
        
        self.session = requests.Session()
        self.session.headers.update({
            "User-Agent": "PolymarketArbBot/1.0",
            "Accept": "application/json"
        })
        
        # Cache
        self._market_cache: Dict[str, BTC15mMarket] = {}
        self._cache_timestamp: float = 0
        self._cache_ttl: float = 30.0  # 30 second cache
        
        # Track discovered event slugs for faster lookup
        self._known_event_slugs: List[str] = []
    
    def _get(self, url: str, params: Optional[dict] = None, timeout: float = 10.0) -> dict:
        """Make GET request with error handling."""
        try:
            response = self.session.get(url, params=params, timeout=timeout)
            response.raise_for_status()
            return response.json()
        except requests.RequestException as e:
            logger.error(f"API request failed: {e}")
            raise
    
    def _search_markets(self, query: str, limit: int = 20) -> List[dict]:
        """Search for markets matching query."""
        url = f"{self.config.gamma_api_url}/markets"
        params = {
            "limit": limit,
            "active": "true",
            "closed": "false",
            "_q": query,  # Search query parameter
        }
        
        try:
            markets = self._get(url, params=params)
            return markets if isinstance(markets, list) else []
        except Exception as e:
            logger.warning(f"Search failed for '{query}': {e}")
            return []
    
    def _search_events(self, query: str, limit: int = 20) -> List[dict]:
        """Search for events matching query."""
        url = f"{self.config.gamma_api_url}/events"
        params = {
            "limit": limit,
            "active": "true",
            "closed": "false",
            "order": "startDate",
            "ascending": "false",
        }
        
        try:
            events = self._get(url, params=params)
            if not isinstance(events, list):
                return []
            
            # Filter by keywords
            keywords = query.lower().split()
            filtered = []
            
            for event in events:
                title = (event.get("title") or "").lower()
                slug = (event.get("slug") or "").lower()
                
                # Check if all keywords match
                if all(kw in title or kw in slug for kw in keywords):
                    filtered.append(event)
            
            return filtered
        except Exception as e:
            logger.warning(f"Event search failed: {e}")
            return []
    
    def _parse_market(self, market_data: dict, event_data: dict = None) -> Optional[BTC15mMarket]:
        """Parse market data into BTC15mMarket object."""
        try:
            # Check if this is a binary market (2 outcomes)
            token_ids_raw = market_data.get("clobTokenIds") or market_data.get("clob_token_ids") or []
            
            if isinstance(token_ids_raw, str):
                try:
                    token_ids = json.loads(token_ids_raw)
                except json.JSONDecodeError:
                    token_ids = []
            else:
                token_ids = token_ids_raw
            
            if not isinstance(token_ids, list) or len(token_ids) != 2:
                return None
            
            yes_token_id = str(token_ids[0])
            no_token_id = str(token_ids[1])
            
            # Parse timestamps
            end_date = market_data.get("endDate") or market_data.get("end_date_iso")
            start_date = market_data.get("startDate") or market_data.get("start_date_iso")
            
            if end_date:
                end_ts = self._parse_timestamp(end_date)
            else:
                end_ts = datetime.now(timezone.utc) + timedelta(minutes=15)
            
            if start_date:
                start_ts = self._parse_timestamp(start_date)
            else:
                start_ts = end_ts - timedelta(minutes=15)
            
            # Generate window ID
            window_id = start_ts.strftime("%Y-%m-%d_%H:%M")
            
            # Check if active and not closed
            active = market_data.get("active", True)
            closed = market_data.get("closed", False)
            
            # Skip if order book disabled
            if not market_data.get("enableOrderBook", True):
                return None
            
            return BTC15mMarket(
                market_id=market_data.get("conditionId") or market_data.get("condition_id", ""),
                event_id=event_data.get("id", "") if event_data else "",
                slug=market_data.get("slug", ""),
                question=market_data.get("question", ""),
                yes_token_id=yes_token_id,
                no_token_id=no_token_id,
                window_id=window_id,
                start_ts=start_ts,
                end_ts=end_ts,
                active=active,
                closed=closed,
                volume_24h=float(market_data.get("volume24hr", 0) or 0),
                liquidity=float(market_data.get("liquidity", 0) or 0),
            )
        
        except Exception as e:
            logger.warning(f"Failed to parse market: {e}")
            return None
    
    def _parse_timestamp(self, ts_str: str) -> datetime:
        """Parse timestamp string to datetime."""
        if not ts_str:
            return datetime.now(timezone.utc)
        
        # Handle various formats
        formats = [
            "%Y-%m-%dT%H:%M:%S.%fZ",
            "%Y-%m-%dT%H:%M:%SZ",
            "%Y-%m-%dT%H:%M:%S",
            "%Y-%m-%d %H:%M:%S",
        ]
        
        for fmt in formats:
            try:
                dt = datetime.strptime(ts_str.replace("+00:00", "Z"), fmt)
                return dt.replace(tzinfo=timezone.utc)
            except ValueError:
                continue
        
        # Fallback
        logger.warning(f"Could not parse timestamp: {ts_str}")
        return datetime.now(timezone.utc)
    
    def _is_btc_15m_market(self, market: dict) -> bool:
        """Check if market matches BTC 15-minute pattern."""
        question = (market.get("question") or "").lower()
        slug = (market.get("slug") or "").lower()
        
        text = f"{question} {slug}"
        
        # Must mention bitcoin/btc
        has_btc = "bitcoin" in text or "btc" in text
        
        # Must mention 15 minute
        has_15m = "15 minute" in text or "15-minute" in text or "15min" in text
        
        # Should mention up/down or higher/lower
        has_direction = any(x in text for x in ["up", "down", "higher", "lower"])
        
        return has_btc and has_15m and has_direction
    
    def discover_all(self, max_results: int = 50) -> List[BTC15mMarket]:
        """
        Discover all BTC 15-minute Up/Down markets.
        
        Returns list sorted by start time (most recent first).
        """
        all_markets = []
        seen_ids = set()
        
        # Search using multiple patterns
        for pattern in self.SEARCH_PATTERNS:
            # Search markets directly
            markets = self._search_markets(pattern, limit=20)
            
            for market_data in markets:
                if not self._is_btc_15m_market(market_data):
                    continue
                
                market = self._parse_market(market_data)
                if market and market.market_id not in seen_ids:
                    all_markets.append(market)
                    seen_ids.add(market.market_id)
            
            # Also search events
            events = self._search_events(pattern, limit=10)
            
            for event in events:
                event_markets = event.get("markets") or []
                for market_data in event_markets:
                    if not self._is_btc_15m_market(market_data):
                        continue
                    
                    market = self._parse_market(market_data, event)
                    if market and market.market_id not in seen_ids:
                        all_markets.append(market)
                        seen_ids.add(market.market_id)
            
            if len(all_markets) >= max_results:
                break
            
            time.sleep(0.1)  # Rate limiting
        
        # Sort by start time (most recent first)
        all_markets.sort(key=lambda m: m.start_ts, reverse=True)
        
        # Update cache
        self._market_cache = {m.market_id: m for m in all_markets}
        self._cache_timestamp = time.time()
        
        logger.info(f"Discovered {len(all_markets)} BTC 15-min markets")
        
        return all_markets[:max_results]
    
    def get_active_market(self) -> Optional[BTC15mMarket]:
        """
        Get the currently active BTC 15-minute market.
        
        Returns the market that is:
        1. Currently within its trading window
        2. Not closed
        3. Has sufficient time remaining
        """
        # Check cache freshness
        if time.time() - self._cache_timestamp > self._cache_ttl:
            self.discover_all()
        
        now = datetime.now(timezone.utc)
        
        # Find active market
        for market in self._market_cache.values():
            if market.closed:
                continue
            
            if market.start_ts <= now <= market.end_ts:
                if market.seconds_remaining > self.config.stop_add_seconds_before_end:
                    return market
        
        # No active market found - try to find upcoming
        upcoming = self.get_next_market()
        if upcoming and upcoming.seconds_remaining <= 30:
            # Window starting soon
            logger.info(f"Next window starting soon: {upcoming.window_id}")
        
        return None
    
    def get_next_market(self) -> Optional[BTC15mMarket]:
        """Get the next upcoming market (not yet started)."""
        # Refresh cache if needed
        if time.time() - self._cache_timestamp > self._cache_ttl:
            self.discover_all()
        
        now = datetime.now(timezone.utc)
        
        # Find earliest upcoming market
        upcoming = None
        for market in self._market_cache.values():
            if market.closed:
                continue
            
            if market.start_ts > now:
                if upcoming is None or market.start_ts < upcoming.start_ts:
                    upcoming = market
        
        return upcoming
    
    def get_market_by_id(self, market_id: str) -> Optional[BTC15mMarket]:
        """Get market by ID from cache."""
        return self._market_cache.get(market_id)
    
    def wait_for_next_window(self, timeout: float = 300) -> Optional[BTC15mMarket]:
        """
        Wait for the next trading window to become active.
        
        Args:
            timeout: Maximum seconds to wait
        
        Returns:
            Active market or None if timeout
        """
        start_time = time.time()
        
        while time.time() - start_time < timeout:
            market = self.get_active_market()
            if market:
                return market
            
            # Check for upcoming window
            upcoming = self.get_next_market()
            if upcoming:
                wait_time = (upcoming.start_ts - datetime.now(timezone.utc)).total_seconds()
                if 0 < wait_time < 60:
                    logger.info(f"Waiting {wait_time:.0f}s for {upcoming.window_id}")
                    time.sleep(min(wait_time + 1, timeout - (time.time() - start_time)))
                    continue
            
            time.sleep(5)
        
        return None


def main():
    """Test market discovery."""
    logging.basicConfig(level=logging.INFO)
    
    discovery = BTC15mMarketDiscovery()
    
    print("\n=== Discovering BTC 15-min Markets ===\n")
    
    markets = discovery.discover_all(max_results=20)
    
    if not markets:
        print("No BTC 15-min markets found.")
        print("\nThis could mean:")
        print("1. No active BTC 15-min markets on Polymarket")
        print("2. Search patterns need adjustment")
        print("3. API changes in Polymarket")
        return
    
    print(f"Found {len(markets)} markets:\n")
    
    for i, market in enumerate(markets[:10]):
        now = datetime.now(timezone.utc)
        status = "ACTIVE" if market.start_ts <= now <= market.end_ts else "ENDED" if now > market.end_ts else "UPCOMING"
        
        print(f"{i+1}. {market.question[:60]}...")
        print(f"   Window: {market.window_id} | Status: {status}")
        print(f"   Start: {market.start_ts.strftime('%Y-%m-%d %H:%M:%S')} UTC")
        print(f"   End:   {market.end_ts.strftime('%Y-%m-%d %H:%M:%S')} UTC")
        print(f"   YES token: {market.yes_token_id[:30]}...")
        print(f"   NO token:  {market.no_token_id[:30]}...")
        print()
    
    # Check for active market
    active = discovery.get_active_market()
    if active:
        print(f"\n=== Currently Active ===")
        print(f"{active.question}")
        print(f"Seconds remaining: {active.seconds_remaining:.0f}")
    else:
        print("\nNo currently active market.")
        
        upcoming = discovery.get_next_market()
        if upcoming:
            wait_time = (upcoming.start_ts - datetime.now(timezone.utc)).total_seconds()
            print(f"Next market in {wait_time:.0f}s: {upcoming.window_id}")


if __name__ == "__main__":
    main()

