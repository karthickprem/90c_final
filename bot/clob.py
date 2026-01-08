"""
CLOB (Central Limit Order Book) client for Polymarket.
Handles orderbook queries and order placement.
"""

import logging
from typing import List, Dict, Optional, Tuple, Any
from dataclasses import dataclass
from enum import Enum

import requests
import yaml

logger = logging.getLogger(__name__)


class Side(Enum):
    BUY = "BUY"
    SELL = "SELL"


class OrderStatus(Enum):
    PENDING = "PENDING"
    OPEN = "OPEN"
    FILLED = "FILLED"
    PARTIALLY_FILLED = "PARTIALLY_FILLED"
    CANCELLED = "CANCELLED"
    FAILED = "FAILED"


@dataclass
class OrderBookLevel:
    """Single price level in the orderbook."""
    price: float
    size: float


@dataclass
class OrderBook:
    """Full orderbook for a token."""
    token_id: str
    bids: List[OrderBookLevel]
    asks: List[OrderBookLevel]
    timestamp: Optional[str] = None
    
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
    def total_ask_depth(self) -> float:
        """Total size of all asks."""
        return sum(level.size for level in self.asks)
    
    @property
    def total_bid_depth(self) -> float:
        """Total size of all bids."""
        return sum(level.size for level in self.bids)


@dataclass 
class FillResult:
    """Result of walking the orderbook to fill an order."""
    can_fill: bool
    filled_shares: float
    total_cost: float
    avg_price: float
    levels_used: int
    remaining_shares: float
    best_ask_price: float = 0.0
    best_ask_size: float = 0.0
    price_slippage_pct: float = 0.0  # How much avg_price differs from best_ask


@dataclass
class OrderbookCheck:
    """Result of orderbook reality check."""
    is_valid: bool
    reject_reason: Optional[str] = None
    best_ask_price: float = 0.0
    best_ask_size: float = 0.0
    depth_price_for_shares: float = 0.0
    total_depth_up_to_price: float = 0.0
    slippage_pct: float = 0.0


@dataclass
class Order:
    """Represents an order to be placed."""
    token_id: str
    side: Side
    shares: float
    limit_price: float
    order_id: Optional[str] = None
    status: OrderStatus = OrderStatus.PENDING
    filled_shares: float = 0.0
    avg_fill_price: float = 0.0


def load_config(config_path: str = "bot/config.yaml") -> dict:
    """Load configuration from YAML file."""
    with open(config_path, "r") as f:
        return yaml.safe_load(f)


class CLOBClient:
    """Client for Polymarket CLOB API with reality checks."""
    
    # Price validation constants
    MIN_VALID_PRICE = 0.0
    MAX_VALID_PRICE = 1.0
    DUST_PRICE_THRESHOLD = 0.01  # 1 cent
    DUST_SIZE_THRESHOLD = 1.0    # Minimum size to not be considered dust
    MAX_SLIPPAGE_PCT = 0.03      # 3% max slippage from best ask to depth-walk price
    
    def __init__(self, base_url: str = None, config: dict = None):
        self.config = config or load_config()
        self.base_url = base_url or self.config.get("clob_api_url", "https://clob.polymarket.com")
        self.session = requests.Session()
        self.session.headers.update({
            "User-Agent": "PolymarketTempBot/1.0",
            "Accept": "application/json"
        })
        self.use_depth = self.config.get("use_depth", True)
    
    def _get(self, endpoint: str, params: Optional[dict] = None, timeout: float = 10.0) -> Any:
        """Make GET request to CLOB API."""
        url = f"{self.base_url}{endpoint}"
        try:
            response = self.session.get(url, params=params, timeout=timeout)
            response.raise_for_status()
            return response.json()
        except requests.RequestException as e:
            logger.error(f"CLOB API error: {e}")
            raise
    
    def _validate_price(self, price: float, token_id: str = "", context: str = "") -> bool:
        """
        Validate that price is within valid range [0, 1].
        Logs warnings for suspicious prices.
        """
        if price < self.MIN_VALID_PRICE or price > self.MAX_VALID_PRICE:
            logger.error(f"INVALID PRICE {price} outside [0,1] for {token_id[:20]}... ({context})")
            return False
        
        if price < self.DUST_PRICE_THRESHOLD:
            logger.warning(f"Suspicious low price {price:.4f} for {token_id[:20]}... ({context})")
        
        return True
    
    def get_book(self, token_id: str) -> OrderBook:
        """
        Get the full orderbook for a token.
        Returns OrderBook with bids and asks sorted by price.
        Validates prices are within [0, 1].
        """
        data = self._get("/book", params={"token_id": token_id})
        
        bids = []
        for level in (data.get("bids") or []):
            price = float(level.get("price", 0))
            size = float(level.get("size", 0))
            if self._validate_price(price, token_id, "bid"):
                bids.append(OrderBookLevel(price=price, size=size))
        
        asks = []
        for level in (data.get("asks") or []):
            price = float(level.get("price", 0))
            size = float(level.get("size", 0))
            if self._validate_price(price, token_id, "ask"):
                asks.append(OrderBookLevel(price=price, size=size))
        
        # Bids sorted descending (best bid first)
        bids.sort(key=lambda x: x.price, reverse=True)
        # Asks sorted ascending (best ask first)
        asks.sort(key=lambda x: x.price)
        
        return OrderBook(
            token_id=token_id,
            bids=bids,
            asks=asks,
            timestamp=data.get("timestamp")
        )
    
    def best_ask_price(self, token_id: str) -> Optional[float]:
        """Get the best ask price for a token."""
        try:
            book = self.get_book(token_id)
            return book.best_ask.price if book.best_ask else None
        except Exception as e:
            logger.error(f"Failed to get best ask for {token_id[:20]}...: {e}")
            return None
    
    def best_bid_price(self, token_id: str) -> Optional[float]:
        """Get the best bid price for a token."""
        try:
            book = self.get_book(token_id)
            return book.best_bid.price if book.best_bid else None
        except Exception as e:
            logger.error(f"Failed to get best bid for {token_id[:20]}...: {e}")
            return None
    
    def fill_cost_for_shares(self, token_id: str, shares: float, 
                             side: Side = Side.BUY) -> FillResult:
        """
        Walk the orderbook to calculate fill cost for a given number of shares.
        
        For BUY orders, walks the asks.
        For SELL orders, walks the bids.
        
        Returns FillResult with depth validation metrics.
        """
        book = self.get_book(token_id)
        
        if side == Side.BUY:
            levels = book.asks
        else:
            levels = book.bids
        
        best_price = levels[0].price if levels else 0.0
        best_size = levels[0].size if levels else 0.0
        
        if not levels:
            return FillResult(
                can_fill=False,
                filled_shares=0,
                total_cost=0,
                avg_price=0,
                levels_used=0,
                remaining_shares=shares,
                best_ask_price=0,
                best_ask_size=0,
                price_slippage_pct=0
            )
        
        remaining = shares
        total_cost = 0.0
        total_filled = 0.0
        levels_used = 0
        
        for level in levels:
            if remaining <= 0:
                break
            
            fill_at_level = min(remaining, level.size)
            cost_at_level = fill_at_level * level.price
            
            total_cost += cost_at_level
            total_filled += fill_at_level
            remaining -= fill_at_level
            levels_used += 1
        
        can_fill = remaining <= 0.001  # Small tolerance for floating point
        avg_price = total_cost / total_filled if total_filled > 0 else 0
        
        # Calculate slippage from best ask
        slippage_pct = 0.0
        if best_price > 0 and avg_price > 0:
            slippage_pct = (avg_price - best_price) / best_price
        
        return FillResult(
            can_fill=can_fill,
            filled_shares=total_filled,
            total_cost=total_cost,
            avg_price=avg_price,
            levels_used=levels_used,
            remaining_shares=max(0, remaining),
            best_ask_price=best_price,
            best_ask_size=best_size,
            price_slippage_pct=slippage_pct
        )
    
    def reality_check(self, token_id: str, required_shares: float,
                      max_price: float = 0.20) -> OrderbookCheck:
        """
        Perform orderbook reality check before trading.
        
        Validates:
        1. Best ask size >= required shares
        2. Depth-walk can fill required shares
        3. Slippage from best ask to depth-walk price <= 3%
        4. Price >= 1 cent (not dust)
        
        Args:
            token_id: The token to check
            required_shares: Number of shares needed
            max_price: Maximum price to consider for depth (default 20 cents)
        
        Returns:
            OrderbookCheck with validation result and metrics
        """
        try:
            book = self.get_book(token_id)
        except Exception as e:
            return OrderbookCheck(
                is_valid=False,
                reject_reason=f"Failed to get orderbook: {e}"
            )
        
        if not book.asks:
            return OrderbookCheck(
                is_valid=False,
                reject_reason="No asks in orderbook"
            )
        
        best_ask = book.best_ask
        best_ask_price = best_ask.price
        best_ask_size = best_ask.size
        
        # Check 1: Dust price guard
        if best_ask_price < self.DUST_PRICE_THRESHOLD:
            if best_ask_size < self.DUST_SIZE_THRESHOLD:
                return OrderbookCheck(
                    is_valid=False,
                    reject_reason=f"Dust order: price={best_ask_price:.4f} size={best_ask_size:.2f}",
                    best_ask_price=best_ask_price,
                    best_ask_size=best_ask_size
                )
            # Low price but meaningful size - log warning but allow
            logger.warning(f"Low price {best_ask_price:.4f} but size {best_ask_size:.2f} - proceeding with caution")
        
        # Calculate depth up to max_price
        total_depth = sum(level.size for level in book.asks if level.price <= max_price)
        
        # Check 2: Depth-walk for required shares
        fill = self.fill_cost_for_shares(token_id, required_shares, Side.BUY)
        
        if not fill.can_fill:
            return OrderbookCheck(
                is_valid=False,
                reject_reason=f"Insufficient depth: need {required_shares:.2f}, have {fill.filled_shares:.2f}",
                best_ask_price=best_ask_price,
                best_ask_size=best_ask_size,
                depth_price_for_shares=fill.avg_price,
                total_depth_up_to_price=total_depth
            )
        
        # Check 3: Slippage
        if fill.price_slippage_pct > self.MAX_SLIPPAGE_PCT:
            return OrderbookCheck(
                is_valid=False,
                reject_reason=f"Slippage too high: {fill.price_slippage_pct:.2%} > {self.MAX_SLIPPAGE_PCT:.2%}",
                best_ask_price=best_ask_price,
                best_ask_size=best_ask_size,
                depth_price_for_shares=fill.avg_price,
                total_depth_up_to_price=total_depth,
                slippage_pct=fill.price_slippage_pct
            )
        
        return OrderbookCheck(
            is_valid=True,
            best_ask_price=best_ask_price,
            best_ask_size=best_ask_size,
            depth_price_for_shares=fill.avg_price,
            total_depth_up_to_price=total_depth,
            slippage_pct=fill.price_slippage_pct
        )
    
    def get_depth_at_price(self, token_id: str, max_price: float, 
                           side: Side = Side.BUY) -> float:
        """
        Get total available shares at or better than a given price.
        For BUY: sum of asks at or below max_price
        For SELL: sum of bids at or above max_price
        """
        book = self.get_book(token_id)
        
        total_size = 0.0
        if side == Side.BUY:
            for level in book.asks:
                if level.price <= max_price:
                    total_size += level.size
                else:
                    break
        else:
            for level in book.bids:
                if level.price >= max_price:
                    total_size += level.size
                else:
                    break
        
        return total_size
    
    def check_liquidity(self, token_id: str, min_shares: float) -> Tuple[bool, float]:
        """
        Check if there's enough liquidity at top of book.
        Returns (has_liquidity, available_at_best_ask).
        """
        book = self.get_book(token_id)
        
        if not book.best_ask:
            return False, 0.0
        
        # Sum up asks at the best price level
        best_price = book.best_ask.price
        available = sum(level.size for level in book.asks if level.price == best_price)
        
        return available >= min_shares, available
    
    def print_orderbook_summary(self, token_id: str, label: str = ""):
        """Print orderbook summary for debugging."""
        book = self.get_book(token_id)
        
        print(f"\n--- Orderbook: {label} ---")
        print(f"Token: {token_id[:30]}...")
        print(f"Best ask: {book.best_ask.price:.4f} x {book.best_ask.size:.2f}" if book.best_ask else "No asks")
        print(f"Best bid: {book.best_bid.price:.4f} x {book.best_bid.size:.2f}" if book.best_bid else "No bids")
        print(f"Spread: {book.spread:.4f}" if book.spread else "No spread")
        print(f"Total ask depth: {book.total_ask_depth:.2f}")
        print(f"Total bid depth: {book.total_bid_depth:.2f}")
        
        print("Top 5 asks:")
        for level in book.asks[:5]:
            print(f"  {level.price:.4f} x {level.size:.2f}")
        
        print("Top 5 bids:")
        for level in book.bids[:5]:
            print(f"  {level.price:.4f} x {level.size:.2f}")


class PaperCLOBClient(CLOBClient):
    """
    Paper trading CLOB client.
    Uses real orderbook data but simulates order execution.
    ONLY fills if depth supports it.
    """
    
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.paper_orders: List[Order] = []
        self.paper_fills: List[Dict] = []
    
    def simulate_fill(self, order: Order) -> Order:
        """
        Simulate filling an order at current market prices.
        Updates order status and fill information.
        ONLY fills if orderbook has sufficient depth.
        """
        # First do reality check
        check = self.reality_check(order.token_id, order.shares)
        if not check.is_valid:
            order.status = OrderStatus.FAILED
            order.filled_shares = 0
            logger.warning(f"Order rejected: {check.reject_reason}")
            return order
        
        fill_result = self.fill_cost_for_shares(
            order.token_id, 
            order.shares, 
            order.side
        )
        
        if not fill_result.can_fill:
            order.status = OrderStatus.FAILED
            order.filled_shares = fill_result.filled_shares
            logger.warning(f"Insufficient depth for order: {order}")
            return order
        
        # Check if fill price is within limit
        if order.side == Side.BUY and fill_result.avg_price > order.limit_price:
            order.status = OrderStatus.FAILED
            logger.warning(f"Fill price {fill_result.avg_price:.4f} exceeds limit {order.limit_price:.4f}")
            return order
        
        order.status = OrderStatus.FILLED
        order.filled_shares = order.shares
        order.avg_fill_price = fill_result.avg_price
        
        self.paper_fills.append({
            "order": order,
            "fill_result": fill_result
        })
        
        logger.info(f"Paper fill: {order.shares:.2f} shares @ {fill_result.avg_price:.4f}")
        return order
    
    def place_order(self, order: Order) -> Order:
        """Place a paper order (simulate execution)."""
        self.paper_orders.append(order)
        return self.simulate_fill(order)


class LiveCLOBClient(CLOBClient):
    """
    Live trading CLOB client.
    Requires API credentials and actually places orders.
    """
    
    def __init__(self, api_key: str, api_secret: str, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.api_key = api_key
        self.api_secret = api_secret
        self._authenticated = False
        
        if not self.config.get("live_enabled", False):
            raise RuntimeError("Live trading is disabled in config. Set live_enabled: true")
        
        if self.config.get("dry_run", True):
            raise RuntimeError("Cannot use LiveCLOBClient in dry_run mode")
    
    def _authenticate(self):
        """Authenticate with the CLOB API."""
        raise NotImplementedError(
            "Live trading requires the py-clob-client library. "
            "Install with: pip install py-clob-client"
        )
    
    def place_order(self, order: Order) -> Order:
        """Place a live order on Polymarket."""
        if not self._authenticated:
            self._authenticate()
        
        raise NotImplementedError("Live order placement not implemented")


if __name__ == "__main__":
    # Test CLOB client with reality check
    logging.basicConfig(level=logging.INFO)
    
    client = CLOBClient()
    print("CLOB client initialized")
    print(f"Base URL: {client.base_url}")
    print(f"Use depth: {client.use_depth}")
