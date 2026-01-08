"""
Market V2 - Correct BTC 15-min Market Handling

Critical fixes:
1. Parse start_ts from slug: btc-updown-15m-{start_ts}
2. end_ts = start_ts + 900 (15 minutes)
3. Use /markets?slug=<slug> for token IDs (not /events)
"""

import logging
import re
import time
import json
import requests
from typing import Optional, Dict, Tuple, List
from dataclasses import dataclass, field
from datetime import datetime, timezone

logger = logging.getLogger(__name__)


@dataclass
class Window15Min:
    """
    A single 15-minute trading window.
    
    The slug format is: btc-updown-15m-{start_timestamp}
    Example: btc-updown-15m-1767503700
        start_ts = 1767503700
        end_ts = 1767503700 + 900 = 1767504600
    """
    slug: str
    start_ts: int  # Unix epoch seconds
    end_ts: int    # start_ts + 900
    
    # Market identifiers
    condition_id: str = ""
    
    # Token IDs (aligned with outcomes)
    up_token_id: str = ""    # Token for "Up" outcome
    down_token_id: str = ""  # Token for "Down" outcome
    
    @classmethod
    def from_slug(cls, slug: str) -> Optional['Window15Min']:
        """
        Parse a 15-min window from its slug.
        
        Slug format: btc-updown-15m-{start_ts}
        """
        # Extract the timestamp from the end of the slug
        match = re.search(r'-(\d{10})$', slug)
        if not match:
            logger.warning(f"Cannot parse start_ts from slug: {slug}")
            return None
        
        start_ts = int(match.group(1))
        end_ts = start_ts + 900  # 15 minutes = 900 seconds
        
        return cls(slug=slug, start_ts=start_ts, end_ts=end_ts)
    
    @property
    def start_dt(self) -> datetime:
        return datetime.fromtimestamp(self.start_ts, tz=timezone.utc)
    
    @property
    def end_dt(self) -> datetime:
        return datetime.fromtimestamp(self.end_ts, tz=timezone.utc)
    
    def seconds_remaining(self) -> float:
        """Seconds until this window ends."""
        now = time.time()
        return max(0, self.end_ts - now)
    
    def is_active(self) -> bool:
        """True if we're currently within this window."""
        now = time.time()
        return self.start_ts <= now < self.end_ts
    
    def is_finished(self) -> bool:
        """True if this window has already ended."""
        return time.time() >= self.end_ts
    
    def is_future(self) -> bool:
        """True if this window hasn't started yet."""
        return time.time() < self.start_ts
    
    def __str__(self):
        status = "ACTIVE" if self.is_active() else ("FINISHED" if self.is_finished() else "FUTURE")
        remaining = self.seconds_remaining()
        return f"Window({self.slug}, {status}, {remaining:.0f}s remaining)"


@dataclass
class OrderBookTick:
    """Single orderbook snapshot for both sides."""
    ts: float  # Unix timestamp
    window: Window15Min
    
    # Up side
    bid_up: float = 0.0
    ask_up: float = 0.0
    size_bid_up: float = 0.0
    size_ask_up: float = 0.0
    
    # Down side
    bid_down: float = 0.0
    ask_down: float = 0.0
    size_bid_down: float = 0.0
    size_ask_down: float = 0.0
    
    @property
    def ask_sum(self) -> float:
        """Sum of best asks (for instant arb check)."""
        return self.ask_up + self.ask_down
    
    @property
    def bid_sum(self) -> float:
        """Sum of best bids."""
        return self.bid_up + self.bid_down
    
    @property
    def spread_up(self) -> float:
        return self.ask_up - self.bid_up if self.bid_up > 0 else 0
    
    @property
    def spread_down(self) -> float:
        return self.ask_down - self.bid_down if self.bid_down > 0 else 0
    
    @property
    def seconds_remaining(self) -> float:
        return max(0, self.window.end_ts - self.ts)
    
    def to_dict(self) -> dict:
        return {
            "ts": self.ts,
            "ts_iso": datetime.fromtimestamp(self.ts, tz=timezone.utc).isoformat(),
            "slug": self.window.slug,
            "start_ts": self.window.start_ts,
            "end_ts": self.window.end_ts,
            "seconds_remaining": self.seconds_remaining,
            "bid_up": self.bid_up,
            "ask_up": self.ask_up,
            "size_bid_up": self.size_bid_up,
            "size_ask_up": self.size_ask_up,
            "bid_down": self.bid_down,
            "ask_down": self.ask_down,
            "size_bid_down": self.size_bid_down,
            "size_ask_down": self.size_ask_down,
            "ask_sum": self.ask_sum,
            "bid_sum": self.bid_sum,
            "spread_up": self.spread_up,
            "spread_down": self.spread_down,
        }


class MarketFetcher:
    """
    Fetches market data from Polymarket APIs.
    
    Uses correct endpoints:
    - Gamma API: /markets?slug=<slug> for market metadata
    - CLOB API: /book for orderbook data
    """
    
    GAMMA_API = "https://gamma-api.polymarket.com"
    CLOB_API = "https://clob.polymarket.com"
    
    def __init__(self):
        self.session = requests.Session()
        self.session.headers.update({
            "User-Agent": "PM-15m-Bot/2.0",
            "Accept": "application/json",
        })
        
        # Cache for market metadata
        self._market_cache: Dict[str, Window15Min] = {}
    
    def _get(self, url: str, params: dict = None, timeout: float = 10) -> Optional[dict]:
        """Make GET request with error handling."""
        try:
            resp = self.session.get(url, params=params, timeout=timeout)
            resp.raise_for_status()
            return resp.json()
        except requests.exceptions.HTTPError as e:
            logger.error(f"HTTP error {e.response.status_code}: {url}")
            return None
        except Exception as e:
            logger.error(f"Request error: {e}")
            return None
    
    def fetch_market_by_slug(self, slug: str) -> Optional[Window15Min]:
        """
        Fetch market metadata using the correct endpoint.
        
        GET https://gamma-api.polymarket.com/markets?slug=<slug>
        
        Returns Window15Min with token IDs populated.
        """
        # Check cache first
        if slug in self._market_cache:
            cached = self._market_cache[slug]
            if cached.up_token_id and cached.down_token_id:
                return cached
        
        # Parse the window from slug first
        window = Window15Min.from_slug(slug)
        if not window:
            return None
        
        # Fetch market metadata
        url = f"{self.GAMMA_API}/markets"
        data = self._get(url, params={"slug": slug})
        
        if not data:
            # Try alternative: search by condition_id pattern
            logger.debug(f"Direct slug lookup failed, trying search...")
            return self._search_market(slug, window)
        
        # Handle both list and single object responses
        markets = data if isinstance(data, list) else [data]
        
        for market in markets:
            if not market:
                continue
            
            market_slug = market.get("slug", "")
            if market_slug != slug:
                continue
            
            # Get outcomes and token IDs
            outcomes_raw = market.get("outcomes", [])
            token_ids_raw = market.get("clobTokenIds", [])
            
            # Parse outcomes if string (API returns JSON string)
            if isinstance(outcomes_raw, str):
                try:
                    outcomes = json.loads(outcomes_raw)
                except:
                    outcomes = []
            else:
                outcomes = outcomes_raw or []
            
            # Parse token IDs if string
            if isinstance(token_ids_raw, str):
                try:
                    token_ids = json.loads(token_ids_raw)
                except:
                    token_ids = []
            else:
                token_ids = token_ids_raw or []
            
            if len(outcomes) != 2 or len(token_ids) != 2:
                logger.warning(f"Unexpected outcomes/tokens: {outcomes}, {token_ids}")
                continue
            
            # Map by outcome name (case-insensitive)
            for i, outcome in enumerate(outcomes):
                outcome_lower = str(outcome).lower()
                if outcome_lower == "up":
                    window.up_token_id = str(token_ids[i])
                elif outcome_lower == "down":
                    window.down_token_id = str(token_ids[i])
            
            # Fallback: assume index 0=Up, 1=Down if names don't match
            if not window.up_token_id and len(token_ids) >= 2:
                window.up_token_id = str(token_ids[0])
                window.down_token_id = str(token_ids[1])
            
            window.condition_id = market.get("conditionId", "")
            
            # Cache it
            self._market_cache[slug] = window
            
            logger.info(f"Fetched market: {slug}")
            logger.info(f"  Up token: {window.up_token_id[:20]}...")
            logger.info(f"  Down token: {window.down_token_id[:20]}...")
            
            return window
        
        return None
    
    def _search_market(self, slug: str, window: Window15Min) -> Optional[Window15Min]:
        """Fallback: search for the market."""
        # Try fetching all recent markets and filtering
        url = f"{self.GAMMA_API}/markets"
        params = {
            "limit": 100,
            "active": "true",
            "closed": "false",
        }
        
        data = self._get(url, params)
        if not data or not isinstance(data, list):
            return None
        
        for market in data:
            market_slug = market.get("slug", "")
            if market_slug == slug or slug in market_slug:
                # Found it
                outcomes_raw = market.get("outcomes", [])
                token_ids_raw = market.get("clobTokenIds", [])
                
                # Parse outcomes if string
                if isinstance(outcomes_raw, str):
                    try:
                        outcomes = json.loads(outcomes_raw)
                    except:
                        outcomes = []
                else:
                    outcomes = outcomes_raw or []
                
                if isinstance(token_ids_raw, str):
                    try:
                        token_ids = json.loads(token_ids_raw)
                    except:
                        token_ids = []
                else:
                    token_ids = token_ids_raw or []
                
                if len(token_ids) >= 2:
                    # Map outcomes
                    for i, outcome in enumerate(outcomes):
                        if str(outcome).lower() == "up":
                            window.up_token_id = str(token_ids[i])
                        elif str(outcome).lower() == "down":
                            window.down_token_id = str(token_ids[i])
                    
                    if not window.up_token_id:
                        window.up_token_id = str(token_ids[0])
                        window.down_token_id = str(token_ids[1])
                    
                    window.condition_id = market.get("conditionId", "")
                    self._market_cache[slug] = window
                    return window
        
        return None
    
    def fetch_orderbook(self, token_id: str) -> Tuple[float, float, float, float]:
        """
        Fetch orderbook for a single token.
        
        Returns (best_bid, best_ask, bid_size, ask_size)
        """
        url = f"{self.CLOB_API}/book"
        data = self._get(url, params={"token_id": token_id})
        
        if not data:
            return (0, 0, 0, 0)
        
        bids = data.get("bids", [])
        asks = data.get("asks", [])
        
        best_bid = 0.0
        bid_size = 0.0
        if bids:
            # Bids sorted by price descending (highest first)
            bids.sort(key=lambda x: float(x.get("price", 0)), reverse=True)
            best_bid = float(bids[0].get("price", 0))
            bid_size = float(bids[0].get("size", 0))
        
        best_ask = 0.0
        ask_size = 0.0
        if asks:
            # Asks sorted by price ascending (lowest first)
            asks.sort(key=lambda x: float(x.get("price", 999)))
            best_ask = float(asks[0].get("price", 0))
            ask_size = float(asks[0].get("size", 0))
        
        return (best_bid, best_ask, bid_size, ask_size)
    
    def fetch_tick(self, window: Window15Min) -> Optional[OrderBookTick]:
        """
        Fetch a complete orderbook tick for both Up and Down.
        """
        if not window.up_token_id or not window.down_token_id:
            logger.error(f"Window missing token IDs: {window}")
            return None
        
        ts = time.time()
        
        # Fetch both orderbooks
        bid_up, ask_up, size_bid_up, size_ask_up = self.fetch_orderbook(window.up_token_id)
        bid_down, ask_down, size_bid_down, size_ask_down = self.fetch_orderbook(window.down_token_id)
        
        return OrderBookTick(
            ts=ts,
            window=window,
            bid_up=bid_up,
            ask_up=ask_up,
            size_bid_up=size_bid_up,
            size_ask_up=size_ask_up,
            bid_down=bid_down,
            ask_down=ask_down,
            size_bid_down=size_bid_down,
            size_ask_down=size_ask_down,
        )


def get_current_window_slug() -> Optional[str]:
    """
    Get the slug for the currently active 15-min window.
    
    Windows start at :00, :15, :30, :45 each hour.
    """
    now = time.time()
    
    # Round down to nearest 15-minute boundary
    start_ts = int(now) // 900 * 900
    
    return f"btc-updown-15m-{start_ts}"


def get_next_window_slug() -> str:
    """Get the slug for the next 15-min window."""
    now = time.time()
    
    # Round up to next 15-minute boundary
    next_start = (int(now) // 900 + 1) * 900
    
    return f"btc-updown-15m-{next_start}"


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(levelname)s | %(message)s")
    
    # Test the market fetcher
    fetcher = MarketFetcher()
    
    # Use the exact slug from the user's URL
    test_slug = "btc-updown-15m-1767503700"
    
    print(f"\n{'='*60}")
    print(f"Testing market fetch for: {test_slug}")
    print(f"{'='*60}\n")
    
    # Parse window
    window = Window15Min.from_slug(test_slug)
    if window:
        print(f"Parsed window:")
        print(f"  Start: {window.start_dt}")
        print(f"  End: {window.end_dt}")
        print(f"  Status: {window}")
    
    # Fetch full market data
    window = fetcher.fetch_market_by_slug(test_slug)
    if window:
        print(f"\nMarket metadata fetched!")
        print(f"  Condition ID: {window.condition_id[:40]}..." if window.condition_id else "  No condition ID")
        print(f"  Up Token: {window.up_token_id[:40]}..." if window.up_token_id else "  No Up token")
        print(f"  Down Token: {window.down_token_id[:40]}..." if window.down_token_id else "  No Down token")
        
        # Fetch tick
        tick = fetcher.fetch_tick(window)
        if tick:
            print(f"\nOrderbook tick:")
            print(f"  Bid Up: {tick.bid_up:.4f} ({tick.size_bid_up:.0f})")
            print(f"  Ask Up: {tick.ask_up:.4f} ({tick.size_ask_up:.0f})")
            print(f"  Bid Down: {tick.bid_down:.4f} ({tick.size_bid_down:.0f})")
            print(f"  Ask Down: {tick.ask_down:.4f} ({tick.size_ask_down:.0f})")
            print(f"  Ask Sum: {tick.ask_sum:.4f}")
            print(f"  Seconds Remaining: {tick.seconds_remaining:.0f}")
    else:
        print("\nFailed to fetch market. Trying current window...")
        
        current_slug = get_current_window_slug()
        print(f"\nCurrent window slug: {current_slug}")
        
        window = fetcher.fetch_market_by_slug(current_slug)
        if window:
            tick = fetcher.fetch_tick(window)
            if tick:
                print(f"\nCurrent window tick:")
                print(f"  Ask Up: {tick.ask_up:.4f}")
                print(f"  Ask Down: {tick.ask_down:.4f}")
                print(f"  Sum: {tick.ask_sum:.4f}")

