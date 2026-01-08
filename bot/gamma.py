"""
Gamma API client for market discovery using public-search endpoint.
Finds temperature bucket markets from Polymarket.
"""

import re
import json
import logging
from typing import List, Dict, Optional, Any, Tuple
from datetime import datetime, date
from dataclasses import dataclass, field

import requests
import yaml

logger = logging.getLogger(__name__)


@dataclass
class TemperatureMarket:
    """Represents a temperature bucket market."""
    market_id: str
    question: str
    slug: str
    yes_token_id: str
    no_token_id: Optional[str]
    tmin_f: float
    tmax_f: float
    target_date: date
    location: str
    enable_order_book: bool
    end_date_iso: Optional[str] = None
    closed: bool = False
    event_id: Optional[str] = None
    event_title: Optional[str] = None
    temp_unit: str = "F"  # Original unit (F or C)
    is_tail_bucket: bool = False  # "or below" / "or higher"
    tail_type: Optional[str] = None  # "lower" or "upper"
    
    @property
    def bucket_width(self) -> float:
        return self.tmax_f - self.tmin_f


@dataclass
class EventWithMarkets:
    """Represents a temperature event with all its bucket markets."""
    event_id: str
    title: str
    slug: str
    location: str
    target_date: date
    markets: List[TemperatureMarket] = field(default_factory=list)
    
    @property
    def bucket_count(self) -> int:
        return len(self.markets)


def load_config(config_path: str = "bot/config.yaml") -> dict:
    """Load configuration from YAML file."""
    with open(config_path, "r") as f:
        return yaml.safe_load(f)


class GammaClient:
    """Client for Polymarket Gamma API using public-search endpoint."""
    
    def __init__(self, base_url: str = None, config: dict = None):
        self.config = config or load_config()
        self.base_url = base_url or self.config.get("gamma_api_url", "https://gamma-api.polymarket.com")
        self.session = requests.Session()
        self.session.headers.update({
            "User-Agent": "PolymarketTempBot/1.0",
            "Accept": "application/json"
        })
        
        # Debug stats
        self.debug_stats = {
            "events_found": 0,
            "markets_found": 0,
            "parse_successes": 0,
            "parse_failures": [],
            "events_by_location": {},
        }
    
    def _get(self, endpoint: str, params: Optional[dict] = None, timeout: float = 15.0) -> Any:
        """Make GET request to Gamma API."""
        url = f"{self.base_url}{endpoint}"
        try:
            response = self.session.get(url, params=params, timeout=timeout)
            response.raise_for_status()
            return response.json()
        except requests.RequestException as e:
            logger.error(f"Gamma API error: {e}")
            raise
    
    def public_search(self, query: str, events_status: str = "active", 
                      page: int = 1, limit: int = 50) -> dict:
        """
        Search using the public-search endpoint.
        Returns events with embedded markets.
        """
        params = {
            "q": query,
            "events_status": events_status,
            "page": page,
        }
        return self._get("/public-search", params=params)
    
    def search_temperature_events(self, 
                                   search_queries: List[str] = None,
                                   events_status: str = "active",
                                   max_pages: int = 5) -> List[dict]:
        """
        Search for temperature events using public-search endpoint.
        Returns list of raw event dictionaries with embedded markets.
        """
        if search_queries is None:
            search_queries = [
                "highest temperature in",
                "temperature",
            ]
        
        all_events = []
        seen_event_ids = set()
        
        for query in search_queries:
            page = 1
            while page <= max_pages:
                try:
                    result = self.public_search(query, events_status, page)
                    
                    events = result.get("events", [])
                    pagination = result.get("pagination", {})
                    has_more = pagination.get("hasMore", False)
                    
                    for event in events:
                        event_id = event.get("id")
                        if event_id and event_id not in seen_event_ids:
                            seen_event_ids.add(event_id)
                            all_events.append(event)
                    
                    if not has_more:
                        break
                    page += 1
                    
                except Exception as e:
                    logger.error(f"Error searching '{query}' page {page}: {e}")
                    break
        
        logger.info(f"Found {len(all_events)} unique temperature events")
        return all_events
    
    def discover_bucket_markets(self, 
                                 location: str = None,
                                 locations: List[str] = None,
                                 date_horizon_days: int = None,
                                 events_status: str = "active",
                                 debug: bool = False) -> List[TemperatureMarket]:
        """
        Discover and parse temperature bucket markets using public-search.
        Returns list of TemperatureMarket objects with parsed data.
        """
        from bot.parse_buckets import parse_temperature_question_v2
        
        # Support single location or list
        if location and not locations:
            locations = [location]
        
        # Normalize locations to lowercase
        if locations:
            locations_lower = [loc.lower().replace(" ", "") for loc in locations]
        else:
            locations_lower = None  # Accept all locations
        
        date_horizon = date_horizon_days or self.config.get("date_horizon_days", 7)
        today = date.today()
        
        # Reset debug stats
        self.debug_stats = {
            "events_found": 0,
            "markets_found": 0,
            "parse_successes": 0,
            "parse_failures": [],
            "events_by_location": {},
        }
        
        # Search for events
        raw_events = self.search_temperature_events(events_status=events_status)
        self.debug_stats["events_found"] = len(raw_events)
        
        parsed_markets = []
        
        for event in raw_events:
            event_id = str(event.get("id", ""))
            event_title = event.get("title", "")
            event_slug = event.get("slug", "")
            markets = event.get("markets", [])
            
            # Skip non-temperature events
            if "temperature" not in event_title.lower():
                continue
            
            self.debug_stats["markets_found"] += len(markets)
            
            for market in markets:
                question = market.get("question", "")
                
                # Parse the question
                parsed = parse_temperature_question_v2(question)
                if not parsed:
                    if debug:
                        self.debug_stats["parse_failures"].append({
                            "question": question[:80],
                            "event": event_title,
                        })
                    continue
                
                tmin_f, tmax_f, target_date, parsed_location, temp_unit, is_tail, tail_type = parsed
                
                # Check date is within horizon
                if target_date:
                    days_ahead = (target_date - today).days
                    if days_ahead < 0 or days_ahead > date_horizon:
                        logger.debug(f"Date {target_date} outside horizon ({days_ahead} days)")
                        continue
                else:
                    continue
                
                # Filter by location if specified
                if locations_lower and parsed_location:
                    loc_normalized = parsed_location.lower().replace(" ", "")
                    # Also check common aliases
                    aliases = {
                        "newyorkcity": "newyork",
                        "nyc": "newyork",
                        "la": "losangeles",
                        "sf": "sanfrancisco",
                    }
                    loc_check = aliases.get(loc_normalized, loc_normalized)
                    
                    if not any(loc_check in l or l in loc_check for l in locations_lower):
                        continue
                
                # Get token IDs (clobTokenIds is a JSON string in public-search response)
                clob_token_ids_raw = market.get("clobTokenIds", [])
                if isinstance(clob_token_ids_raw, str):
                    try:
                        clob_token_ids = json.loads(clob_token_ids_raw)
                    except json.JSONDecodeError:
                        clob_token_ids = []
                else:
                    clob_token_ids = clob_token_ids_raw or []
                
                if len(clob_token_ids) < 1:
                    logger.warning(f"No CLOB token IDs for market: {question[:50]}")
                    continue
                
                yes_token_id = str(clob_token_ids[0])
                no_token_id = str(clob_token_ids[1]) if len(clob_token_ids) > 1 else None
                
                # Check orderbook is enabled
                enable_order_book = market.get("enableOrderBook", True)
                if not enable_order_book:
                    continue
                
                self.debug_stats["parse_successes"] += 1
                
                # Track by location
                loc_key = (parsed_location or "Unknown").title()
                if loc_key not in self.debug_stats["events_by_location"]:
                    self.debug_stats["events_by_location"][loc_key] = 0
                self.debug_stats["events_by_location"][loc_key] += 1
                
                temp_market = TemperatureMarket(
                    market_id=market.get("id", ""),
                    question=question,
                    slug=market.get("slug", ""),
                    yes_token_id=yes_token_id,
                    no_token_id=no_token_id,
                    tmin_f=tmin_f,
                    tmax_f=tmax_f,
                    target_date=target_date,
                    location=loc_key,
                    enable_order_book=enable_order_book,
                    end_date_iso=market.get("endDate"),
                    closed=market.get("closed", False),
                    event_id=event_id,
                    event_title=event_title,
                    temp_unit=temp_unit,
                    is_tail_bucket=is_tail,
                    tail_type=tail_type,
                )
                parsed_markets.append(temp_market)
                
                if debug:
                    logger.info(f"Parsed: {tmin_f:.0f}-{tmax_f:.0f}F on {target_date} ({parsed_location})")
        
        logger.info(f"Discovered {len(parsed_markets)} valid temperature bucket markets")
        return parsed_markets
    
    def discover_events_with_markets(self,
                                      location: str = None,
                                      locations: List[str] = None,
                                      date_horizon_days: int = None,
                                      events_status: str = "active",
                                      debug: bool = False) -> List[EventWithMarkets]:
        """
        Discover temperature events and group their markets.
        Returns list of EventWithMarkets objects.
        """
        markets = self.discover_bucket_markets(
            location=location,
            locations=locations,
            date_horizon_days=date_horizon_days,
            events_status=events_status,
            debug=debug,
        )
        
        # Group by event
        events_map: Dict[str, EventWithMarkets] = {}
        
        for market in markets:
            event_id = market.event_id or "unknown"
            
            if event_id not in events_map:
                events_map[event_id] = EventWithMarkets(
                    event_id=event_id,
                    title=market.event_title or "",
                    slug=market.slug.rsplit("-", 1)[0] if "-" in market.slug else market.slug,
                    location=market.location,
                    target_date=market.target_date,
                    markets=[],
                )
            
            events_map[event_id].markets.append(market)
        
        # Sort markets within each event by tmin
        for event in events_map.values():
            event.markets.sort(key=lambda m: m.tmin_f)
        
        return list(events_map.values())
    
    def print_debug_stats(self):
        """Print debug statistics from last discovery run."""
        stats = self.debug_stats
        print("\n" + "="*60)
        print("MARKET DISCOVERY DEBUG STATS (public-search)")
        print("="*60)
        print(f"\nEvents found: {stats['events_found']}")
        print(f"Markets in events: {stats['markets_found']}")
        print(f"Successfully parsed: {stats['parse_successes']}")
        print(f"Parse failures: {len(stats['parse_failures'])}")
        
        print(f"\nMarkets by location:")
        for loc, count in sorted(stats["events_by_location"].items()):
            print(f"  {loc}: {count}")
        
        if stats["parse_failures"]:
            print(f"\nSample parse failures:")
            for fail in stats["parse_failures"][:5]:
                print(f"  Event: {fail['event']}")
                print(f"    Q: {fail['question']}")
        
        print()


def group_markets_by_date(markets: List[TemperatureMarket]) -> Dict[date, List[TemperatureMarket]]:
    """Group temperature markets by their target date."""
    groups: Dict[date, List[TemperatureMarket]] = {}
    for market in markets:
        if market.target_date not in groups:
            groups[market.target_date] = []
        groups[market.target_date].append(market)
    
    # Sort buckets within each date by tmin
    for d in groups:
        groups[d].sort(key=lambda m: m.tmin_f)
    
    return groups


def group_markets_by_location_date(markets: List[TemperatureMarket]) -> Dict[Tuple[str, date], List[TemperatureMarket]]:
    """Group temperature markets by (location, date)."""
    groups: Dict[Tuple[str, date], List[TemperatureMarket]] = {}
    for market in markets:
        key = (market.location.lower(), market.target_date)
        if key not in groups:
            groups[key] = []
        groups[key].append(market)
    
    # Sort buckets within each group by tmin
    for key in groups:
        groups[key].sort(key=lambda m: m.tmin_f)
    
    return groups


if __name__ == "__main__":
    # Test market discovery with debug output
    logging.basicConfig(level=logging.INFO)
    
    client = GammaClient()
    
    print("Scanning for temperature markets using public-search...")
    events = client.discover_events_with_markets(
        locations=None,  # All locations
        debug=True
    )
    
    client.print_debug_stats()
    
    print(f"\nTotal temperature events: {len(events)}")
    
    for event in events:
        print(f"\n{event.location} on {event.target_date}: {event.bucket_count} buckets")
        print(f"  Title: {event.title}")
        for m in event.markets[:3]:
            tail = f" [{m.tail_type}]" if m.is_tail_bucket else ""
            print(f"    {m.tmin_f:.0f}-{m.tmax_f:.0f}F{tail} | token={m.yes_token_id[:12]}...")
        if len(event.markets) > 3:
            print(f"    ... and {len(event.markets) - 3} more buckets")
