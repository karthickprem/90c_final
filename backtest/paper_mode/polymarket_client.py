"""
Polymarket Client Adapter

Wraps API functions for paper mode with robust discovery and caching.

PAPER MODE ONLY - NO TRADING CAPABILITY
=======================================
This client is READ-ONLY. It cannot:
- Place orders
- Cancel orders
- Sign transactions
- Access private keys

All methods use only public GET endpoints.
"""

from __future__ import annotations

import json
import time
import asyncio
import aiohttp
from typing import Dict, Optional, List
from dataclasses import dataclass, field
from datetime import datetime


# PAPER MODE SAFETY GUARD
_PAPER_MODE = True

def _assert_paper_mode():
    if not _PAPER_MODE:
        raise RuntimeError("FATAL: Paper mode disabled")


# API Configuration
GAMMA_API = "https://gamma-api.polymarket.com"
CLOB_HOST = "https://clob.polymarket.com"

# Timeouts (short to avoid blocking)
CONNECT_TIMEOUT = 2.0
READ_TIMEOUT = 2.0

# Discovery settings
DISCOVERY_CACHE_TTL = 300  # 5 minutes
DISCOVERY_RETRY_INTERVAL = 30  # seconds
MAX_PRICE_FAILURES = 10  # Invalidate cache after N failures


@dataclass
class WindowInfo:
    """Information about current 15-min window."""
    slug: str
    start_ts: int
    end_ts: int
    secs_left: int
    time_str: str


@dataclass 
class PriceData:
    """Price data for both sides (legacy midpoint)."""
    up_cents: int
    down_cents: int
    ts: float


@dataclass
class QuoteData:
    """
    Full bid/ask quote data for both sides.
    
    All prices in integer cents [0-100].
    """
    # UP side
    up_bid: int  # Best bid to sell UP
    up_ask: int  # Best ask to buy UP
    up_mid: int  # Midpoint
    
    # DOWN side
    down_bid: int
    down_ask: int
    down_mid: int
    
    # Metadata
    ts: float
    is_synthetic: bool = False  # True if computed from midpoint + spread
    
    @property
    def up_spread(self) -> int:
        return self.up_ask - self.up_bid
    
    @property
    def down_spread(self) -> int:
        return self.down_ask - self.down_bid
    
    def to_price_data(self) -> PriceData:
        """Convert to legacy PriceData using midpoints."""
        return PriceData(
            up_cents=self.up_mid,
            down_cents=self.down_mid,
            ts=self.ts
        )


@dataclass
class MarketInfo:
    """Discovered market information."""
    market_id: str
    condition_id: str
    slug: str
    up_token_id: str
    down_token_id: str
    question: str
    discovered_at: float = field(default_factory=time.time)
    
    def is_stale(self, ttl: float = DISCOVERY_CACHE_TTL) -> bool:
        return time.time() - self.discovered_at > ttl


class PolymarketClient:
    """
    Polymarket API client for paper mode.
    
    Features:
    - Robust market discovery with multiple strategies
    - Token caching with TTL
    - Auto-retry on failures
    - Short timeouts to avoid blocking
    
    PAPER MODE ONLY - All trading methods raise RuntimeError.
    """
    
    def __init__(self):
        _assert_paper_mode()
        self.session: Optional[aiohttp.ClientSession] = None
        self.tokens: Dict[str, str] = {}
        self.current_slug: Optional[str] = None
        self.condition_id: Optional[str] = None
        
        # Discovery cache
        self._cached_market: Optional[MarketInfo] = None
        self._last_discovery_attempt: float = 0
        self._consecutive_failures: int = 0
        self._discovery_in_progress: bool = False
    
    # =========================================================================
    # TRADING METHODS - DISABLED
    # =========================================================================
    
    def place_order(self, *args, **kwargs):
        raise RuntimeError("PAPER MODE: place_order disabled")
    
    def cancel_order(self, *args, **kwargs):
        raise RuntimeError("PAPER MODE: cancel_order disabled")
    
    def execute_trade(self, *args, **kwargs):
        raise RuntimeError("PAPER MODE: execute_trade disabled")
    
    def post_order(self, *args, **kwargs):
        raise RuntimeError("PAPER MODE: post_order disabled")
    
    # =========================================================================
    # CONNECTION
    # =========================================================================
    
    async def connect(self) -> None:
        """Initialize HTTP session with short timeouts."""
        if self.session is None:
            timeout = aiohttp.ClientTimeout(
                connect=CONNECT_TIMEOUT,
                sock_read=READ_TIMEOUT,
            )
            self.session = aiohttp.ClientSession(timeout=timeout)
    
    async def close(self) -> None:
        """Close HTTP session."""
        if self.session:
            await self.session.close()
            self.session = None
    
    # =========================================================================
    # WINDOW TIMING
    # =========================================================================
    
    def get_window(self) -> WindowInfo:
        """Get current 15-min window info."""
        ts = int(time.time())
        start = ts - (ts % 900)
        end = start + 900
        secs_left = end - ts
        
        return WindowInfo(
            slug=f"btc-updown-15m-{start}",
            start_ts=start,
            end_ts=end,
            secs_left=secs_left,
            time_str=f"{secs_left // 60}:{secs_left % 60:02d}",
        )
    
    # =========================================================================
    # MARKET DISCOVERY (robust multi-strategy)
    # =========================================================================
    
    async def resolve_btc15_market(self, force: bool = False) -> Optional[MarketInfo]:
        """
        Discover BTC 15m Up/Down market.
        
        Uses multiple strategies:
        1. Check cache (if not stale)
        2. Try known slug patterns
        3. Search all active markets by keywords
        
        Args:
            force: Force rediscovery even if cache is fresh
        
        Returns:
            MarketInfo if found, None otherwise
        """
        # Check cache first
        if not force and self._cached_market and not self._cached_market.is_stale():
            return self._cached_market
        
        # Rate limit discovery attempts
        now = time.time()
        if not force and now - self._last_discovery_attempt < DISCOVERY_RETRY_INTERVAL:
            return self._cached_market
        
        # Prevent concurrent discovery
        if self._discovery_in_progress:
            return self._cached_market
        
        self._discovery_in_progress = True
        self._last_discovery_attempt = now
        
        try:
            await self.connect()
            
            # Strategy 1: Try known slug patterns
            market = await self._discover_by_slugs()
            if market:
                return self._cache_market(market)
            
            # Strategy 2: Search active markets
            market = await self._discover_by_search()
            if market:
                return self._cache_market(market)
            
            # All strategies failed
            self._consecutive_failures += 1
            return None
        
        finally:
            self._discovery_in_progress = False
    
    async def _discover_by_slugs(self) -> Optional[MarketInfo]:
        """Try known slug patterns."""
        ts = int(time.time())
        start = ts - (ts % 900)
        
        slugs = [
            f"btc-updown-15m-{start}",
            f"will-btc-go-up-or-down-15m-{start}",
            f"btc-15m-{start}",
            "btc-15-minute-up-or-down",
        ]
        
        for slug in slugs:
            try:
                async with self.session.get(
                    f"{GAMMA_API}/markets",
                    params={"slug": slug},
                ) as resp:
                    if resp.status == 200:
                        data = await resp.json()
                        if data and len(data) > 0:
                            market = self._parse_market(data[0])
                            if market and market.up_token_id and market.down_token_id:
                                return market
            except:
                pass
        
        return None
    
    async def _discover_by_search(self) -> Optional[MarketInfo]:
        """Search all active markets for BTC 15m."""
        try:
            async with self.session.get(
                f"{GAMMA_API}/markets",
                params={"closed": "false", "active": "true", "limit": "200"},
            ) as resp:
                if resp.status != 200:
                    return None
                
                markets = await resp.json()
                
                # Score and filter candidates
                candidates = []
                for m in markets:
                    q = m.get("question", "").lower()
                    s = m.get("slug", "").lower()
                    
                    is_btc = "btc" in q or "btc" in s or "bitcoin" in q
                    is_15m = "15" in q or "15m" in s or "15 min" in q
                    is_updown = "up" in q or "down" in q
                    
                    if is_btc and (is_15m or is_updown):
                        score = sum([is_btc, is_15m, is_updown])
                        candidates.append((score, m))
                
                candidates.sort(key=lambda x: -x[0])
                
                # Try to find one with valid tokens
                for _, m in candidates:
                    market = self._parse_market(m)
                    if market and market.up_token_id and market.down_token_id:
                        return market
        except:
            pass
        
        return None
    
    def _parse_market(self, m: Dict) -> Optional[MarketInfo]:
        """Parse market dict into MarketInfo."""
        try:
            toks = m.get("clobTokenIds", [])
            outs = m.get("outcomes", [])
            
            if isinstance(toks, str):
                toks = json.loads(toks)
            if isinstance(outs, str):
                outs = json.loads(outs)
            
            if len(toks) < 2 or len(outs) < 2:
                return None
            
            token_map = {o.lower(): t for o, t in zip(outs, toks)}
            
            up_token = token_map.get("up") or token_map.get("yes")
            down_token = token_map.get("down") or token_map.get("no")
            
            if not up_token or not down_token:
                return None
            
            return MarketInfo(
                market_id=m.get("id", ""),
                condition_id=m.get("conditionId", ""),
                slug=m.get("slug", ""),
                up_token_id=up_token,
                down_token_id=down_token,
                question=m.get("question", ""),
            )
        except:
            return None
    
    def _cache_market(self, market: MarketInfo) -> MarketInfo:
        """Cache discovered market and apply to client state."""
        self._cached_market = market
        self._consecutive_failures = 0
        
        self.tokens = {
            "up": market.up_token_id,
            "down": market.down_token_id,
        }
        self.current_slug = market.slug
        self.condition_id = market.condition_id
        
        return market
    
    def invalidate_cache(self) -> None:
        """Force cache invalidation."""
        self._cached_market = None
        self.tokens = {}
        self._last_discovery_attempt = 0
    
    # =========================================================================
    # TOKEN FETCHING (legacy interface)
    # =========================================================================
    
    async def fetch_tokens(self, slug: str) -> Dict[str, str]:
        """Fetch tokens - uses cached discovery."""
        if self.tokens:
            return self.tokens
        
        market = await self.resolve_btc15_market()
        return self.tokens if market else {}
    
    async def ensure_tokens(self) -> bool:
        """Ensure we have valid tokens."""
        if self.tokens:
            return True
        
        market = await self.resolve_btc15_market(force=True)
        return market is not None
    
    # =========================================================================
    # PRICE FETCHING
    # =========================================================================
    
    async def fetch_midpoint(self, token_id: str) -> float:
        """Fetch midpoint price for a token."""
        if not self.session:
            await self.connect()
        
        try:
            async with self.session.get(
                f"{CLOB_HOST}/midpoint",
                params={"token_id": token_id},
            ) as resp:
                if resp.status == 200:
                    data = await resp.json()
                    return float(data.get("mid", 0))
                elif resp.status == 429:
                    # Rate limited
                    return -1
        except:
            pass
        
        return 0
    
    async def fetch_prices(self) -> PriceData:
        """
        Fetch current prices for both UP and DOWN (midpoint only).
        
        DEPRECATED: Use fetch_quotes() for tradable bid/ask prices.
        
        Returns prices in cents (0-100).
        """
        # Ensure we have tokens
        if not self.tokens:
            await self.ensure_tokens()
        
        if not self.tokens:
            return PriceData(up_cents=0, down_cents=0, ts=time.time())
        
        # Fetch in parallel
        up_task = self.fetch_midpoint(self.tokens.get("up", ""))
        down_task = self.fetch_midpoint(self.tokens.get("down", ""))
        
        try:
            up_price, down_price = await asyncio.gather(up_task, down_task)
            
            # Check for rate limiting
            if up_price == -1 or down_price == -1:
                self._consecutive_failures += 1
            elif up_price == 0 and down_price == 0:
                self._consecutive_failures += 1
            else:
                self._consecutive_failures = 0
            
            # Invalidate cache if too many failures
            if self._consecutive_failures > MAX_PRICE_FAILURES:
                self.invalidate_cache()
            
            up_cents = max(0, int(up_price * 100)) if up_price > 0 else 0
            down_cents = max(0, int(down_price * 100)) if down_price > 0 else 0
            
            return PriceData(
                up_cents=up_cents,
                down_cents=down_cents,
                ts=time.time(),
            )
        except:
            return PriceData(up_cents=0, down_cents=0, ts=time.time())
    
    async def fetch_book(self, token_id: str) -> tuple[int, int, int]:
        """
        Fetch orderbook for a token and return (bid, ask, mid) in cents.
        
        Returns (0, 0, 0) on failure.
        """
        if not self.session:
            await self.connect()
        
        try:
            async with self.session.get(
                f"{CLOB_HOST}/book",
                params={"token_id": token_id},
            ) as resp:
                if resp.status == 200:
                    data = await resp.json()
                    
                    # Parse bids (sorted descending, best first)
                    bids = data.get("bids", [])
                    best_bid = 0
                    if bids:
                        best_bid = int(float(bids[0].get("price", 0)) * 100)
                    
                    # Parse asks (sorted ascending, best first)
                    asks = data.get("asks", [])
                    best_ask = 0
                    if asks:
                        best_ask = int(float(asks[0].get("price", 0)) * 100)
                    
                    # Calculate mid
                    mid = (best_bid + best_ask) // 2 if (best_bid and best_ask) else 0
                    
                    return (best_bid, best_ask, mid)
                    
                elif resp.status == 429:
                    self._consecutive_failures += 1
                    return (-1, -1, -1)  # Rate limited
        except Exception as e:
            pass
        
        return (0, 0, 0)
    
    async def fetch_quotes(self, use_synthetic: bool = False, default_spread: int = 2) -> QuoteData:
        """
        Fetch full bid/ask quotes for both UP and DOWN.
        
        Args:
            use_synthetic: If True and book fetch fails, compute from midpoint + spread
            default_spread: Default spread in cents for synthetic quotes
        
        Returns:
            QuoteData with bid/ask for both sides in cents (0-100).
        """
        # Ensure we have tokens
        if not self.tokens:
            await self.ensure_tokens()
        
        if not self.tokens:
            return QuoteData(
                up_bid=0, up_ask=0, up_mid=0,
                down_bid=0, down_ask=0, down_mid=0,
                ts=time.time(), is_synthetic=True
            )
        
        # Fetch books in parallel
        up_task = self.fetch_book(self.tokens.get("up", ""))
        down_task = self.fetch_book(self.tokens.get("down", ""))
        
        try:
            (up_bid, up_ask, up_mid), (down_bid, down_ask, down_mid) = await asyncio.gather(
                up_task, down_task
            )
            
            # Check for rate limiting
            if up_bid == -1 or down_bid == -1:
                self._consecutive_failures += 1
            elif up_bid == 0 and down_bid == 0 and up_ask == 0 and down_ask == 0:
                self._consecutive_failures += 1
            else:
                self._consecutive_failures = 0
            
            # Invalidate cache if too many failures
            if self._consecutive_failures > MAX_PRICE_FAILURES:
                self.invalidate_cache()
            
            # Use synthetic if book is empty but synthetic allowed
            is_synthetic = False
            
            if use_synthetic and (up_bid <= 0 or up_ask <= 0):
                # Fallback to midpoint with synthetic spread
                mid = await self.fetch_midpoint(self.tokens.get("up", ""))
                if mid > 0:
                    up_mid = int(mid * 100)
                    half_spread = default_spread // 2
                    up_bid = max(1, up_mid - half_spread)
                    up_ask = min(99, up_mid + half_spread)
                    is_synthetic = True
            
            if use_synthetic and (down_bid <= 0 or down_ask <= 0):
                mid = await self.fetch_midpoint(self.tokens.get("down", ""))
                if mid > 0:
                    down_mid = int(mid * 100)
                    half_spread = default_spread // 2
                    down_bid = max(1, down_mid - half_spread)
                    down_ask = min(99, down_mid + half_spread)
                    is_synthetic = True
            
            return QuoteData(
                up_bid=max(0, up_bid) if up_bid > 0 else 0,
                up_ask=max(0, up_ask) if up_ask > 0 else 0,
                up_mid=max(0, up_mid) if up_mid > 0 else 0,
                down_bid=max(0, down_bid) if down_bid > 0 else 0,
                down_ask=max(0, down_ask) if down_ask > 0 else 0,
                down_mid=max(0, down_mid) if down_mid > 0 else 0,
                ts=time.time(),
                is_synthetic=is_synthetic,
            )
            
        except Exception as e:
            return QuoteData(
                up_bid=0, up_ask=0, up_mid=0,
                down_bid=0, down_ask=0, down_mid=0,
                ts=time.time(), is_synthetic=True
            )
    
    # =========================================================================
    # RESOLUTION
    # =========================================================================
    
    async def get_market_winner(self, slug: str) -> Optional[str]:
        """Get market winner after resolution."""
        if not self.session:
            await self.connect()
        
        try:
            async with self.session.get(
                f"{GAMMA_API}/markets",
                params={"slug": slug},
            ) as resp:
                if resp.status == 200:
                    markets = await resp.json()
                    if markets:
                        m = markets[0]
                        
                        outcomes = m.get("outcomes", [])
                        outcome_prices = m.get("outcomePrices", [])
                        
                        if isinstance(outcomes, str):
                            outcomes = json.loads(outcomes)
                        if isinstance(outcome_prices, str):
                            outcome_prices = json.loads(outcome_prices)
                        
                        for i, price in enumerate(outcome_prices):
                            if float(price) >= 0.99:
                                if i < len(outcomes):
                                    return outcomes[i].lower()
        except:
            pass
        
        return None
    
    async def wait_for_resolution(self, slug: str, max_wait: int = 180) -> Optional[str]:
        """Wait for market to resolve."""
        start = time.time()
        
        while time.time() - start < max_wait:
            winner = await self.get_market_winner(slug)
            if winner:
                return winner
            await asyncio.sleep(2)
        
        return None
