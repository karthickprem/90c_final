"""
Orderbook Fetcher and Normalization

Fetches and normalizes orderbook data from Polymarket CLOB.
Provides top-of-book prices and depth for arbitrage calculations.

Key features:
- Fetch best ask YES/NO with sizes
- Full depth arrays for slippage simulation
- VWAP calculation for order sizing
- Staleness tracking
"""

import logging
import time
from typing import Optional, List, Dict, Tuple, Any
from dataclasses import dataclass, field
from datetime import datetime, timezone
import requests

from .config import ArbConfig, load_config

logger = logging.getLogger(__name__)


@dataclass
class OrderBookLevel:
    """Single price level in orderbook."""
    price: float  # Price in dollars (0-1)
    size: float   # Size in shares


@dataclass
class VWAPResult:
    """Result of VWAP calculation."""
    vwap: float           # Volume-weighted average price
    total_cost: float     # Total cost to fill
    filled_shares: float  # Shares that can be filled
    can_fill: bool        # Whether full size can be filled
    levels_used: int      # Number of price levels consumed
    worst_price: float    # Worst price in the fill


@dataclass
class OrderBookSnapshot:
    """
    Snapshot of orderbook for a token at a point in time.
    """
    token_id: str
    side: str  # "YES" or "NO"
    bids: List[OrderBookLevel]
    asks: List[OrderBookLevel]
    timestamp: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    fetch_time_ms: float = 0  # Time to fetch this book
    
    @property
    def best_bid(self) -> Optional[OrderBookLevel]:
        return self.bids[0] if self.bids else None
    
    @property
    def best_ask(self) -> Optional[OrderBookLevel]:
        return self.asks[0] if self.asks else None
    
    @property
    def spread(self) -> Optional[float]:
        if self.best_bid and self.best_ask:
            return self.best_ask.price - self.best_bid.price
        return None
    
    @property
    def mid_price(self) -> Optional[float]:
        if self.best_bid and self.best_ask:
            return (self.best_ask.price + self.best_bid.price) / 2
        return None
    
    @property
    def staleness_ms(self) -> float:
        """How old is this snapshot in milliseconds."""
        return (datetime.now(timezone.utc) - self.timestamp).total_seconds() * 1000
    
    @property
    def is_stale(self) -> bool:
        """Check if snapshot is too old (> 2 seconds)."""
        return self.staleness_ms > 2000
    
    def vwap_buy(self, shares: float) -> VWAPResult:
        """Calculate VWAP to BUY given number of shares (walks asks)."""
        return self._calculate_vwap(self.asks, shares)
    
    def vwap_sell(self, shares: float) -> VWAPResult:
        """Calculate VWAP to SELL given number of shares (walks bids)."""
        return self._calculate_vwap(self.bids, shares)
    
    def _calculate_vwap(self, levels: List[OrderBookLevel], shares: float) -> VWAPResult:
        """Walk price levels to compute VWAP for given size."""
        if not levels or shares <= 0:
            return VWAPResult(
                vwap=0, total_cost=0, filled_shares=0,
                can_fill=False, levels_used=0, worst_price=0
            )
        
        remaining = shares
        total_cost = 0.0
        filled = 0.0
        levels_used = 0
        worst_price = levels[0].price
        
        for level in levels:
            if remaining <= 0:
                break
            
            fill_at_level = min(remaining, level.size)
            cost_at_level = fill_at_level * level.price
            
            total_cost += cost_at_level
            filled += fill_at_level
            remaining -= fill_at_level
            levels_used += 1
            worst_price = level.price
        
        can_fill = remaining <= 0.001  # Small tolerance
        vwap = total_cost / filled if filled > 0 else 0
        
        return VWAPResult(
            vwap=vwap,
            total_cost=total_cost,
            filled_shares=filled,
            can_fill=can_fill,
            levels_used=levels_used,
            worst_price=worst_price
        )
    
    def to_dict(self) -> dict:
        """Convert to dictionary for serialization."""
        return {
            "token_id": self.token_id,
            "side": self.side,
            "bids": [{"price": l.price, "size": l.size} for l in self.bids],
            "asks": [{"price": l.price, "size": l.size} for l in self.asks],
            "timestamp": self.timestamp.isoformat(),
            "fetch_time_ms": self.fetch_time_ms,
        }
    
    @classmethod
    def from_dict(cls, data: dict) -> "OrderBookSnapshot":
        """Create from dictionary."""
        bids = [OrderBookLevel(price=l["price"], size=l["size"]) for l in data.get("bids", [])]
        asks = [OrderBookLevel(price=l["price"], size=l["size"]) for l in data.get("asks", [])]
        
        ts = data.get("timestamp")
        if isinstance(ts, str):
            timestamp = datetime.fromisoformat(ts.replace("Z", "+00:00"))
        else:
            timestamp = datetime.now(timezone.utc)
        
        return cls(
            token_id=data.get("token_id", ""),
            side=data.get("side", ""),
            bids=bids,
            asks=asks,
            timestamp=timestamp,
            fetch_time_ms=data.get("fetch_time_ms", 0),
        )


@dataclass
class TickData:
    """
    Combined tick data for both YES and NO sides.
    This is what the strategy engine works with.
    """
    timestamp: datetime
    market_id: str
    window_id: str
    
    # YES side
    ask_yes: float
    ask_yes_size: float
    bid_yes: float
    bid_yes_size: float
    
    # NO side
    ask_no: float
    ask_no_size: float
    bid_no: float
    bid_no_size: float
    
    # Optional orderbook snapshots (defaults must come last)
    yes_book: Optional[OrderBookSnapshot] = None
    no_book: Optional[OrderBookSnapshot] = None
    
    # Calculated fields
    @property
    def sum_asks(self) -> float:
        """Sum of best asks (raw pair cost at L1)."""
        return self.ask_yes + self.ask_no
    
    @property
    def min_depth(self) -> float:
        """Minimum depth at best asks."""
        return min(self.ask_yes_size, self.ask_no_size)
    
    @property
    def staleness_ms(self) -> float:
        """Staleness of the older book."""
        if self.yes_book and self.no_book:
            return max(self.yes_book.staleness_ms, self.no_book.staleness_ms)
        return 0
    
    def vwap_pair_cost(self, shares: float) -> Tuple[float, bool]:
        """
        Calculate VWAP-based pair cost for given share quantity.
        
        Returns (pair_cost, can_fill_both).
        """
        if not self.yes_book or not self.no_book:
            return self.sum_asks, False
        
        yes_vwap = self.yes_book.vwap_buy(shares)
        no_vwap = self.no_book.vwap_buy(shares)
        
        can_fill = yes_vwap.can_fill and no_vwap.can_fill
        pair_cost = yes_vwap.vwap + no_vwap.vwap
        
        return pair_cost, can_fill


class OrderbookFetcher:
    """
    Fetches and normalizes orderbook data from Polymarket CLOB.
    
    Provides:
    - Top-of-book prices and sizes
    - Full depth arrays for VWAP calculation
    - Parallel fetching for YES and NO
    """
    
    def __init__(self, config: ArbConfig = None):
        self.config = config or load_config()
        
        self.session = requests.Session()
        self.session.headers.update({
            "User-Agent": "PolymarketArbBot/1.0",
            "Accept": "application/json"
        })
        
        # Metrics
        self.fetches_total = 0
        self.fetches_failed = 0
        self.avg_fetch_time_ms = 0
    
    def _fetch_book(self, token_id: str, side: str) -> Optional[OrderBookSnapshot]:
        """
        Fetch orderbook for a single token.
        
        Args:
            token_id: Polymarket token ID
            side: "YES" or "NO" (for labeling)
        
        Returns:
            OrderBookSnapshot or None if failed
        """
        try:
            start_time = time.time()
            
            url = f"{self.config.clob_api_url}/book"
            response = self.session.get(
                url, 
                params={"token_id": token_id},
                timeout=5
            )
            response.raise_for_status()
            
            fetch_time_ms = (time.time() - start_time) * 1000
            
            data = response.json()
            
            # Parse bids (sorted descending by price)
            bids = []
            for level in (data.get("bids") or []):
                price = float(level.get("price", 0))
                size = float(level.get("size", 0))
                if 0 < price <= 1 and size > 0:
                    bids.append(OrderBookLevel(price=price, size=size))
            bids.sort(key=lambda x: x.price, reverse=True)
            
            # Parse asks (sorted ascending by price)
            asks = []
            for level in (data.get("asks") or []):
                price = float(level.get("price", 0))
                size = float(level.get("size", 0))
                if 0 < price <= 1 and size > 0:
                    asks.append(OrderBookLevel(price=price, size=size))
            asks.sort(key=lambda x: x.price)
            
            self.fetches_total += 1
            
            # Update rolling average
            alpha = 0.1
            self.avg_fetch_time_ms = alpha * fetch_time_ms + (1 - alpha) * self.avg_fetch_time_ms
            
            return OrderBookSnapshot(
                token_id=token_id,
                side=side,
                bids=bids,
                asks=asks,
                timestamp=datetime.now(timezone.utc),
                fetch_time_ms=fetch_time_ms,
            )
        
        except Exception as e:
            self.fetches_failed += 1
            logger.warning(f"Failed to fetch book for {side} ({token_id[:20]}...): {e}")
            return None
    
    def fetch_top_of_book(self, yes_token_id: str, no_token_id: str,
                          market_id: str = "", window_id: str = "") -> Optional[TickData]:
        """
        Fetch top-of-book for both YES and NO tokens.
        
        This is the main method called by the strategy engine.
        
        Args:
            yes_token_id: Token ID for YES outcome
            no_token_id: Token ID for NO outcome
            market_id: Optional market ID for logging
            window_id: Optional window ID for logging
        
        Returns:
            TickData with both sides, or None if either fails
        """
        # Fetch both books
        yes_book = self._fetch_book(yes_token_id, "YES")
        no_book = self._fetch_book(no_token_id, "NO")
        
        if not yes_book or not no_book:
            return None
        
        if not yes_book.best_ask or not no_book.best_ask:
            logger.warning("Missing asks in orderbook")
            return None
        
        return TickData(
            timestamp=datetime.now(timezone.utc),
            market_id=market_id,
            window_id=window_id,
            # YES side
            ask_yes=yes_book.best_ask.price,
            ask_yes_size=yes_book.best_ask.size,
            bid_yes=yes_book.best_bid.price if yes_book.best_bid else 0,
            bid_yes_size=yes_book.best_bid.size if yes_book.best_bid else 0,
            yes_book=yes_book,
            # NO side
            ask_no=no_book.best_ask.price,
            ask_no_size=no_book.best_ask.size,
            bid_no=no_book.best_bid.price if no_book.best_bid else 0,
            bid_no_size=no_book.best_bid.size if no_book.best_bid else 0,
            no_book=no_book,
        )
    
    def fetch_full_depth(self, yes_token_id: str, no_token_id: str) -> Tuple[Optional[OrderBookSnapshot], Optional[OrderBookSnapshot]]:
        """
        Fetch full orderbook depth for both tokens.
        
        Returns (yes_book, no_book) tuple.
        """
        yes_book = self._fetch_book(yes_token_id, "YES")
        no_book = self._fetch_book(no_token_id, "NO")
        return yes_book, no_book
    
    def get_stats(self) -> Dict[str, Any]:
        """Get fetcher statistics."""
        return {
            "fetches_total": self.fetches_total,
            "fetches_failed": self.fetches_failed,
            "failure_rate": self.fetches_failed / max(1, self.fetches_total),
            "avg_fetch_time_ms": round(self.avg_fetch_time_ms, 2),
        }


class MockOrderbookFetcher:
    """
    Mock orderbook fetcher for testing and replay.
    Returns pre-configured tick data.
    """
    
    def __init__(self, ticks: List[TickData] = None):
        self.ticks = ticks or []
        self.tick_index = 0
    
    def add_tick(self, tick: TickData):
        """Add a tick to the queue."""
        self.ticks.append(tick)
    
    def fetch_top_of_book(self, *args, **kwargs) -> Optional[TickData]:
        """Return next tick in queue."""
        if self.tick_index >= len(self.ticks):
            return None
        
        tick = self.ticks[self.tick_index]
        self.tick_index += 1
        return tick
    
    def reset(self):
        """Reset to beginning."""
        self.tick_index = 0


if __name__ == "__main__":
    # Test orderbook fetcher
    logging.basicConfig(level=logging.INFO)
    
    fetcher = OrderbookFetcher()
    
    print("\n=== Testing Orderbook Fetcher ===\n")
    
    # We need actual token IDs to test
    # For now, just show the fetcher is working
    print(f"CLOB URL: {fetcher.config.clob_api_url}")
    print(f"Average fetch time: {fetcher.avg_fetch_time_ms}ms")
    
    # Test with a known token if available
    # This would need real token IDs from market discovery
    print("\nTo test, run market_discovery.py first to get token IDs.")

