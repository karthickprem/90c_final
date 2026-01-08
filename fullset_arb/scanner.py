"""
Arbitrage Scanner Module

Scans binary markets for full-set mispricing opportunities:
  edge_buy = 1.0 - (askYES + askNO)

If edge_buy > min_edge, we can buy both YES and NO for less than $1,
guaranteeing a profit when one side settles to $1.

This is the "real arb" - no directional risk, only execution risk.

CRITICAL: Uses VWAP (volume-weighted average price) across depth levels,
not just top-of-book, to compute executable edge for actual order sizes.
"""

import logging
import time
from typing import List, Dict, Optional, Tuple
from dataclasses import dataclass, field
from datetime import datetime, timedelta
import requests

from .config import ArbConfig, load_config
from .market_discovery import MarketDiscovery, BinaryMarket

logger = logging.getLogger(__name__)

# Maximum staleness for orderbook data (seconds)
MAX_BOOK_STALENESS_SEC = 2.0


@dataclass
class OrderBookLevel:
    """Single price level in orderbook."""
    price: float
    size: float


@dataclass
class VWAPResult:
    """Result of VWAP calculation across depth."""
    vwap: float           # Volume-weighted average price
    total_cost: float     # Total cost to fill
    filled_shares: float  # Shares that can be filled
    can_fill: bool        # Whether full size can be filled
    levels_used: int      # Number of price levels consumed
    worst_price: float    # Worst price in the fill


@dataclass
class OrderBookSnapshot:
    """Snapshot of orderbook for a token."""
    token_id: str
    bids: List[OrderBookLevel]
    asks: List[OrderBookLevel]
    timestamp: datetime = field(default_factory=datetime.now)
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
    def staleness_sec(self) -> float:
        """How old is this snapshot in seconds."""
        return (datetime.now() - self.timestamp).total_seconds()
    
    @property
    def is_stale(self) -> bool:
        """Check if snapshot is too old to use."""
        return self.staleness_sec > MAX_BOOK_STALENESS_SEC
    
    def vwap_buy(self, shares: float) -> VWAPResult:
        """
        Calculate VWAP to BUY given number of shares.
        Walks the asks from best to worst.
        """
        return self._calculate_vwap(self.asks, shares)
    
    def vwap_sell(self, shares: float) -> VWAPResult:
        """
        Calculate VWAP to SELL given number of shares.
        Walks the bids from best to worst.
        """
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


@dataclass
class ArbOpportunity:
    """
    Detected arbitrage opportunity.
    
    Core signals:
    - edge_l1 = 1.0 - (ask_yes + ask_no) at top-of-book (L1)
    - edge_exec = 1.0 - (vwap_yes + vwap_no) - buffers (executable edge)
    
    Only edge_exec matters for actionability!
    """
    
    # Market info
    market: BinaryMarket
    timestamp: datetime
    
    # L1 prices (top-of-book)
    ask_yes: float
    ask_no: float
    bid_yes: float
    bid_no: float
    
    # VWAP prices for target size
    vwap_yes: float
    vwap_no: float
    vwap_bid_yes: float  # For unwind estimation
    vwap_bid_no: float   # For unwind estimation
    target_shares: float  # Shares we priced for
    
    # Calculated edges
    edge_l1: float       # 1.0 - (ask_yes + ask_no) - top of book only
    edge_exec: float     # 1.0 - (vwap_yes + vwap_no) - fee_buffer - slippage_buffer
    
    # Depth at best ask
    depth_yes: float
    depth_no: float
    min_depth: float
    
    # Spreads
    spread_yes: float
    spread_no: float
    
    # Staleness
    staleness_yes_ms: float
    staleness_no_ms: float
    
    # Actionability (based on edge_exec, NOT edge_l1)
    is_actionable: bool
    reject_reason: Optional[str] = None
    
    # Strategy B2: sum_bids > 1 (sell-side arb)
    edge_sell: float = 0  # (bid_yes + bid_no) - 1
    edge_sell_exec: float = 0  # VWAP-based sell edge
    
    # Execution info (filled later)
    executed: bool = False
    execution_result: Optional[dict] = None
    
    @property
    def sum_asks(self) -> float:
        return self.ask_yes + self.ask_no
    
    @property
    def sum_bids(self) -> float:
        return self.bid_yes + self.bid_no
    
    @property
    def sum_vwap_asks(self) -> float:
        return self.vwap_yes + self.vwap_no
    
    @property
    def max_shares_at_top(self) -> float:
        """Max shares we can buy at top-of-book prices."""
        return self.min_depth
    
    def to_dict(self) -> dict:
        """Convert to dictionary for logging."""
        return {
            "market_slug": self.market.slug,
            "market_question": self.market.question[:80] if self.market.question else "",
            "timestamp": self.timestamp.isoformat(),
            "ask_yes": self.ask_yes,
            "ask_no": self.ask_no,
            "bid_yes": self.bid_yes,
            "bid_no": self.bid_no,
            "vwap_yes": self.vwap_yes,
            "vwap_no": self.vwap_no,
            "target_shares": self.target_shares,
            "edge_l1": self.edge_l1,
            "edge_exec": self.edge_exec,
            "edge_sell": self.edge_sell,
            "depth_yes": self.depth_yes,
            "depth_no": self.depth_no,
            "min_depth": self.min_depth,
            "spread_yes": self.spread_yes,
            "spread_no": self.spread_no,
            "staleness_yes_ms": self.staleness_yes_ms,
            "staleness_no_ms": self.staleness_no_ms,
            "is_actionable": self.is_actionable,
            "reject_reason": self.reject_reason,
        }


class ArbScanner:
    """
    Scans markets for full-set arbitrage opportunities.
    
    Core logic:
    1. For each binary market, fetch orderbook for YES and NO tokens
    2. Compute edge = 1.0 - (askYES + askNO)
    3. If edge > min_edge and depth/spread filters pass, signal opportunity
    
    Filters applied:
    - min_edge: Minimum edge after fees
    - max_spread_each: Maximum bid-ask spread per side
    - min_top_depth: Minimum shares at best ask
    """
    
    def __init__(self, config: ArbConfig = None, discovery: MarketDiscovery = None):
        self.config = config or load_config()
        self.discovery = discovery or MarketDiscovery(self.config)
        
        self.session = requests.Session()
        self.session.headers.update({
            "User-Agent": "PolymarketArbBot/1.0",
            "Accept": "application/json"
        })
        
        # Track disabled markets (after bad unwinds)
        self._disabled_markets: Dict[str, float] = {}  # market_id -> disabled_until
        
        # Edge distribution tracking for analysis
        self._edge_distribution: List[Dict] = []
        
        # Stats
        self.scans_total = 0
        self.opportunities_found = 0
        self.actionable_found = 0
    
    def _get_orderbook(self, token_id: str) -> Optional[OrderBookSnapshot]:
        """
        Fetch orderbook for a token from CLOB API.
        Tracks fetch time for staleness monitoring.
        """
        try:
            start_time = time.time()
            url = f"{self.config.clob_api_url}/book"
            response = self.session.get(url, params={"token_id": token_id}, timeout=10)
            response.raise_for_status()
            fetch_time_ms = (time.time() - start_time) * 1000
            
            data = response.json()
            
            bids = []
            for level in (data.get("bids") or []):
                price = float(level.get("price", 0))
                size = float(level.get("size", 0))
                if 0 < price <= 1:
                    bids.append(OrderBookLevel(price=price, size=size))
            
            asks = []
            for level in (data.get("asks") or []):
                price = float(level.get("price", 0))
                size = float(level.get("size", 0))
                if 0 < price <= 1:
                    asks.append(OrderBookLevel(price=price, size=size))
            
            # Sort bids descending, asks ascending
            bids.sort(key=lambda x: x.price, reverse=True)
            asks.sort(key=lambda x: x.price)
            
            return OrderBookSnapshot(
                token_id=token_id,
                bids=bids,
                asks=asks,
                timestamp=datetime.now(),
                fetch_time_ms=fetch_time_ms
            )
            
        except Exception as e:
            logger.debug(f"Failed to get orderbook for {token_id[:20]}...: {e}")
            return None
    
    def is_market_disabled(self, market_id: str) -> bool:
        """Check if market is temporarily disabled."""
        if market_id not in self._disabled_markets:
            return False
        
        disabled_until = self._disabled_markets[market_id]
        if time.time() > disabled_until:
            del self._disabled_markets[market_id]
            return False
        
        return True
    
    def disable_market(self, market_id: str, minutes: int = None):
        """Temporarily disable a market after bad unwind."""
        minutes = minutes or self.config.market_disable_minutes
        self._disabled_markets[market_id] = time.time() + (minutes * 60)
        logger.warning(f"Disabled market {market_id} for {minutes} minutes")
    
    def scan_market(self, market: BinaryMarket) -> Optional[ArbOpportunity]:
        """
        Scan a single market for arbitrage opportunity.
        
        Uses VWAP (volume-weighted average price) across depth levels
        to compute executable edge, not just top-of-book.
        
        Returns ArbOpportunity if any edge exists (even if not actionable).
        Returns None if no edge or orderbook unavailable.
        """
        # Skip disabled markets
        if self.is_market_disabled(market.market_id):
            return None
        
        # Fetch orderbooks for both tokens
        yes_book = self._get_orderbook(market.yes_token_id)
        no_book = self._get_orderbook(market.no_token_id)
        
        if not yes_book or not no_book:
            return None
        
        # Check for staleness
        if yes_book.is_stale or no_book.is_stale:
            logger.debug(f"Stale orderbook for {market.slug}")
            return None
        
        # Check for valid asks
        if not yes_book.best_ask or not no_book.best_ask:
            return None
        
        # Check for valid bids (needed for unwind estimation)
        if not yes_book.best_bid or not no_book.best_bid:
            return None
        
        # L1 prices (top-of-book)
        ask_yes = yes_book.best_ask.price
        ask_no = no_book.best_ask.price
        bid_yes = yes_book.best_bid.price
        bid_no = no_book.best_bid.price
        
        depth_yes = yes_book.best_ask.size
        depth_no = no_book.best_ask.size
        
        spread_yes = yes_book.spread or 0
        spread_no = no_book.spread or 0
        
        # Calculate target shares based on order size
        cost_per_set_l1 = ask_yes + ask_no
        if cost_per_set_l1 <= 0:
            return None
        target_shares = self.config.order_size_usd / cost_per_set_l1
        
        # VWAP calculation for target size (BUY side - asks)
        vwap_yes_result = yes_book.vwap_buy(target_shares)
        vwap_no_result = no_book.vwap_buy(target_shares)
        
        # VWAP for unwind estimation (SELL side - bids)
        vwap_bid_yes_result = yes_book.vwap_sell(target_shares)
        vwap_bid_no_result = no_book.vwap_sell(target_shares)
        
        vwap_yes = vwap_yes_result.vwap if vwap_yes_result.can_fill else ask_yes
        vwap_no = vwap_no_result.vwap if vwap_no_result.can_fill else ask_no
        vwap_bid_yes = vwap_bid_yes_result.vwap if vwap_bid_yes_result.can_fill else bid_yes
        vwap_bid_no = vwap_bid_no_result.vwap if vwap_bid_no_result.can_fill else bid_no
        
        # L1 edge (for logging/comparison only)
        edge_l1 = 1.0 - (ask_yes + ask_no)
        
        # EXECUTABLE edge: uses VWAP + fee buffer + slippage buffer
        total_fee = 2 * self.config.taker_fee_fraction
        slippage_buffer = self.config.max_slippage_pct
        edge_exec = 1.0 - (vwap_yes + vwap_no) - total_fee - slippage_buffer
        
        # Strategy B2: sum_bids > 1 (sell-side arb - requires inventory)
        edge_sell = (bid_yes + bid_no) - 1.0
        edge_sell_exec = (vwap_bid_yes + vwap_bid_no) - 1.0 - total_fee - slippage_buffer
        
        # Staleness in milliseconds
        staleness_yes_ms = yes_book.staleness_sec * 1000
        staleness_no_ms = no_book.staleness_sec * 1000
        
        # Determine if actionable (based on edge_exec, NOT edge_l1)
        is_actionable = True
        reject_reason = None
        
        # Filter 1: Minimum EXECUTABLE edge (the critical check!)
        if edge_exec < self.config.min_edge:
            is_actionable = False
            reject_reason = f"Edge_exec {edge_exec:.4f} < min {self.config.min_edge:.4f}"
        
        # Filter 2: Must be able to fill target size
        elif not vwap_yes_result.can_fill:
            is_actionable = False
            reject_reason = f"Cannot fill {target_shares:.1f} YES shares"
        
        elif not vwap_no_result.can_fill:
            is_actionable = False
            reject_reason = f"Cannot fill {target_shares:.1f} NO shares"
        
        # Filter 3: Max spread per side
        elif spread_yes > self.config.max_spread_each:
            is_actionable = False
            reject_reason = f"YES spread {spread_yes:.4f} > max {self.config.max_spread_each:.4f}"
        
        elif spread_no > self.config.max_spread_each:
            is_actionable = False
            reject_reason = f"NO spread {spread_no:.4f} > max {self.config.max_spread_each:.4f}"
        
        # Filter 4: Minimum depth at top-of-book
        elif depth_yes < self.config.min_top_depth:
            is_actionable = False
            reject_reason = f"YES depth {depth_yes:.1f} < min {self.config.min_top_depth:.1f}"
        
        elif depth_no < self.config.min_top_depth:
            is_actionable = False
            reject_reason = f"NO depth {depth_no:.1f} < min {self.config.min_top_depth:.1f}"
        
        # Only return if there's some edge worth logging (L1)
        # We log even non-actionable for distribution analysis
        if edge_l1 <= -0.1:
            return None
        
        return ArbOpportunity(
            market=market,
            timestamp=datetime.now(),
            # L1 prices
            ask_yes=ask_yes,
            ask_no=ask_no,
            bid_yes=bid_yes,
            bid_no=bid_no,
            # VWAP prices
            vwap_yes=vwap_yes,
            vwap_no=vwap_no,
            vwap_bid_yes=vwap_bid_yes,
            vwap_bid_no=vwap_bid_no,
            target_shares=target_shares,
            # Edges
            edge_l1=edge_l1,
            edge_exec=edge_exec,
            # Sell-side arb (B2)
            edge_sell=edge_sell,
            edge_sell_exec=edge_sell_exec,
            # Depth
            depth_yes=depth_yes,
            depth_no=depth_no,
            min_depth=min(depth_yes, depth_no),
            # Spreads
            spread_yes=spread_yes,
            spread_no=spread_no,
            # Staleness
            staleness_yes_ms=staleness_yes_ms,
            staleness_no_ms=staleness_no_ms,
            # Actionability
            is_actionable=is_actionable,
            reject_reason=reject_reason,
        )
    
    def scan_all(self, markets: List[BinaryMarket] = None) -> List[ArbOpportunity]:
        """
        Scan all markets for arbitrage opportunities.
        
        Returns list of opportunities sorted by edge_exec (highest first).
        Also logs top-K edges for distribution analysis.
        """
        self.scans_total += 1
        
        if markets is None:
            markets = self.discovery.get_cached_markets()
        
        opportunities = []
        
        for market in markets:
            try:
                opp = self.scan_market(market)
                if opp:
                    opportunities.append(opp)
                    
                    if opp.edge_exec > 0:
                        self.opportunities_found += 1
                    
                    if opp.is_actionable:
                        self.actionable_found += 1
                        logger.info(
                            f"ACTIONABLE: {market.slug} | "
                            f"edge_l1={opp.edge_l1:.4f} edge_exec={opp.edge_exec:.4f} | "
                            f"vwap_sum={opp.sum_vwap_asks:.4f} | "
                            f"depth={opp.min_depth:.0f}"
                        )
                
                # Small delay to avoid rate limiting
                time.sleep(0.05)
                
            except Exception as e:
                logger.warning(f"Error scanning {market.slug}: {e}")
                continue
        
        # Sort by edge_exec (highest first) - the executable edge
        opportunities.sort(key=lambda x: x.edge_exec, reverse=True)
        
        # Log top-K for distribution analysis
        self._log_top_edges(opportunities)
        
        return opportunities
    
    def _log_top_edges(self, opportunities: List[ArbOpportunity], top_k: int = 20):
        """Log top-K edges for distribution analysis."""
        if not opportunities:
            return
        
        # Best edge this cycle
        best = opportunities[0]
        logger.info(
            f"BEST EDGE: {best.market.slug[:40]} | "
            f"edge_l1={best.edge_l1:.4f} edge_exec={best.edge_exec:.4f} | "
            f"sum_asks={best.sum_asks:.4f} sum_vwap={best.sum_vwap_asks:.4f}"
        )
        
        # Store for histogram analysis
        self._edge_distribution.append({
            "timestamp": datetime.now().isoformat(),
            "best_edge_l1": best.edge_l1,
            "best_edge_exec": best.edge_exec,
            "best_sum_asks": best.sum_asks,
            "best_market": best.market.slug,
            "num_positive_l1": sum(1 for o in opportunities if o.edge_l1 > 0),
            "num_positive_exec": sum(1 for o in opportunities if o.edge_exec > 0),
            "num_actionable": sum(1 for o in opportunities if o.is_actionable),
            # Top-K edges
            "top_k_edges_l1": [o.edge_l1 for o in opportunities[:top_k]],
            "top_k_edges_exec": [o.edge_exec for o in opportunities[:top_k]],
        })
        
        # Keep only last 1000 entries
        if len(self._edge_distribution) > 1000:
            self._edge_distribution = self._edge_distribution[-1000:]
    
    def get_edge_distribution(self) -> List[Dict]:
        """Get edge distribution data for analysis."""
        return self._edge_distribution.copy()
    
    def get_actionable_opportunities(self, markets: List[BinaryMarket] = None) -> List[ArbOpportunity]:
        """Get only actionable opportunities (based on edge_exec >= min_edge)."""
        all_opps = self.scan_all(markets)
        return [opp for opp in all_opps if opp.is_actionable]
    
    def get_sell_opportunities(self, markets: List[BinaryMarket] = None) -> List[ArbOpportunity]:
        """Get Strategy B2 opportunities (sum_bids > 1)."""
        all_opps = self.scan_all(markets)
        return [opp for opp in all_opps if opp.edge_sell_exec >= self.config.min_edge]
    
    def get_stats(self) -> Dict:
        """Get scanner statistics."""
        return {
            "scans_total": self.scans_total,
            "opportunities_found": self.opportunities_found,
            "actionable_found": self.actionable_found,
            "disabled_markets": len(self._disabled_markets),
            "edge_distribution_samples": len(self._edge_distribution),
        }


def main():
    """Test scanner with VWAP-based edge calculation."""
    logging.basicConfig(level=logging.INFO)
    
    config = ArbConfig()
    config.min_edge = 0.006  # Use proper positive threshold
    
    scanner = ArbScanner(config)
    
    print("\n=== Discovering Markets ===")
    markets = scanner.discovery.discover_all(max_markets=50)
    
    print(f"\n=== Scanning {len(markets)} Markets (VWAP-based) ===\n")
    opportunities = scanner.scan_all(markets)
    
    # Show top opportunities (sorted by edge_exec)
    print("\n=== Top 20 Opportunities (by edge_exec) ===\n")
    for i, opp in enumerate(opportunities[:20]):
        status = "[OK] ACTIONABLE" if opp.is_actionable else f"[X] {opp.reject_reason}"
        print(f"{i+1:3}. {opp.market.slug[:40]}")
        print(f"     L1:   askYES={opp.ask_yes:.4f} askNO={opp.ask_no:.4f} sum={opp.sum_asks:.4f}")
        print(f"     VWAP: vwapYES={opp.vwap_yes:.4f} vwapNO={opp.vwap_no:.4f} sum={opp.sum_vwap_asks:.4f}")
        print(f"     Edge: L1={opp.edge_l1:.4f} EXEC={opp.edge_exec:.4f}")
        print(f"     Sell: edge_sell={opp.edge_sell:.4f} edge_sell_exec={opp.edge_sell_exec:.4f}")
        print(f"     Depth: YES={opp.depth_yes:.0f} NO={opp.depth_no:.0f}")
        print(f"     Status: {status}")
        print()
    
    print(f"\n=== Stats ===")
    print(f"Total scans: {scanner.scans_total}")
    print(f"Opportunities with positive edge_exec: {scanner.opportunities_found}")
    print(f"Actionable opportunities: {scanner.actionable_found}")
    
    # Show edge distribution
    dist = scanner.get_edge_distribution()
    if dist:
        latest = dist[-1]
        print(f"\n=== Latest Edge Distribution ===")
        print(f"Best edge_l1: {latest['best_edge_l1']:.4f}")
        print(f"Best edge_exec: {latest['best_edge_exec']:.4f}")
        print(f"Num positive L1: {latest['num_positive_l1']}")
        print(f"Num positive exec: {latest['num_positive_exec']}")
        print(f"Num actionable: {latest['num_actionable']}")


if __name__ == "__main__":
    main()

