"""
Market Resolution
=================
Resolve BTC 15-min market token IDs.
"""

import time
import json
import requests
from typing import Optional, Dict, Tuple
from dataclasses import dataclass

from .config import Config


@dataclass
class MarketInfo:
    """Market information"""
    slug: str
    yes_token_id: str
    no_token_id: str
    condition_id: str
    question: str
    end_time: int  # Unix timestamp
    
    @property
    def secs_left(self) -> int:
        return max(0, self.end_time - int(time.time()))
    
    @property
    def time_str(self) -> str:
        secs = self.secs_left
        return f"{secs // 60}:{secs % 60:02d}"


class MarketResolver:
    """
    Resolve BTC 15-min market tokens.
    
    Uses direct token IDs from config if available,
    otherwise resolves from Gamma API.
    """
    
    def __init__(self, config: Config):
        self.config = config
        self._cache: Dict[str, MarketInfo] = {}
        self._cache_time: Dict[str, float] = {}
        self._cache_ttl = 60.0  # Cache for 60 seconds
    
    def get_current_window(self) -> Dict:
        """Get current 15-min window info"""
        ts = int(time.time())
        start = ts - (ts % 900)
        end = start + 900
        
        return {
            "slug": f"btc-updown-15m-{start}",
            "start": start,
            "end": end,
            "secs_left": end - ts
        }
    
    def resolve_market(self, slug: Optional[str] = None) -> Optional[MarketInfo]:
        """
        Resolve market tokens for current or specified window.
        
        If config has explicit token IDs, use those.
        Otherwise, fetch from Gamma API.
        """
        if slug is None:
            window = self.get_current_window()
            slug = window["slug"]
        
        # Check cache
        if slug in self._cache:
            if time.time() - self._cache_time.get(slug, 0) < self._cache_ttl:
                return self._cache[slug]
        
        # Try config first (most reliable)
        if self.config.market.yes_token_id and self.config.market.no_token_id:
            # Construct market info from config
            window = self.get_current_window()
            info = MarketInfo(
                slug=slug,
                yes_token_id=self.config.market.yes_token_id,
                no_token_id=self.config.market.no_token_id,
                condition_id="",
                question=f"BTC 15-min: {slug}",
                end_time=window["end"]
            )
            self._cache[slug] = info
            self._cache_time[slug] = time.time()
            return info
        
        # Fetch from Gamma API
        return self._fetch_from_api(slug)
    
    def _fetch_from_api(self, slug: str) -> Optional[MarketInfo]:
        """Fetch market info from Gamma API"""
        try:
            url = f"{self.config.api.gamma_host}/markets"
            params = {"slug": slug}
            
            resp = requests.get(url, params=params, timeout=5)
            if resp.status_code != 200:
                return None
            
            markets = resp.json()
            if not markets:
                return None
            
            m = markets[0]
            
            # Parse token IDs
            tokens = m.get("clobTokenIds", [])
            outcomes = m.get("outcomes", [])
            
            if isinstance(tokens, str):
                tokens = json.loads(tokens)
            if isinstance(outcomes, str):
                outcomes = json.loads(outcomes)
            
            if len(tokens) < 2 or len(outcomes) < 2:
                return None
            
            # Map outcome names to tokens
            token_map = {o.lower(): t for o, t in zip(outcomes, tokens)}
            
            yes_token = token_map.get("up") or token_map.get("yes")
            no_token = token_map.get("down") or token_map.get("no")
            
            if not yes_token or not no_token:
                return None
            
            # Parse end time from slug
            try:
                start_ts = int(slug.split("-")[-1])
                end_ts = start_ts + 900
            except:
                end_ts = int(time.time()) + 900
            
            info = MarketInfo(
                slug=slug,
                yes_token_id=yes_token,
                no_token_id=no_token,
                condition_id=m.get("conditionId", ""),
                question=m.get("question", slug),
                end_time=end_ts
            )
            
            self._cache[slug] = info
            self._cache_time[slug] = time.time()
            
            return info
        
        except Exception as e:
            if self.config.verbose:
                print(f"[MARKET] Error fetching market: {e}")
            return None
    
    def get_token_ids(self) -> Tuple[Optional[str], Optional[str]]:
        """Get (yes_token_id, no_token_id) for current window"""
        market = self.resolve_market()
        if market:
            return market.yes_token_id, market.no_token_id
        return None, None

