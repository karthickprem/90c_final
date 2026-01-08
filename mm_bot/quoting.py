"""
Quote Computation
=================
Compute bid/ask quotes with inventory skew and risk limits.
"""

import math
from typing import Optional, Tuple
from dataclasses import dataclass

from .config import Config, QuotingParams
from .clob import OrderBook


@dataclass
class Quote:
    """A computed quote"""
    price: float
    size: float
    side: str  # "BUY" or "SELL"
    
    # Metadata
    reason: Optional[str] = None
    skew_applied: float = 0.0


@dataclass
class QuotePair:
    """Bid and ask quotes for a token"""
    bid: Optional[Quote] = None
    ask: Optional[Quote] = None
    
    # Market data used
    best_bid: float = 0.0
    best_ask: float = 0.0
    mid: float = 0.0
    spread: float = 0.0


def round_to_tick(price: float, tick: float = 0.01) -> float:
    """Round price to nearest tick"""
    return round(round(price / tick) * tick, 2)


def clamp_price(price: float, min_p: float = 0.01, max_p: float = 0.99) -> float:
    """Clamp price to valid range"""
    return max(min_p, min(max_p, price))


class QuoteEngine:
    """
    Compute quotes based on:
    - Current order book
    - Inventory position
    - Risk limits
    - Target spread
    """
    
    def __init__(self, config: Config):
        self.config = config
        self.params = config.quoting
    
    def compute_quotes(
        self,
        book: OrderBook,
        inventory_shares: float,
        max_inventory: float,
        usdc_available: float = 1000.0
    ) -> QuotePair:
        """
        Compute bid and ask quotes for a token.
        
        Args:
            book: Current order book
            inventory_shares: Current position in this token
            max_inventory: Maximum allowed inventory
            usdc_available: Available USDC for buying
        
        Returns:
            QuotePair with bid and ask quotes
        """
        result = QuotePair(
            best_bid=book.best_bid,
            best_ask=book.best_ask,
            mid=book.mid,
            spread=book.spread
        )
        
        # Don't quote if spread is too tight (no edge)
        min_spread = self.params.min_half_spread_cents * 2 / 100
        if book.spread < min_spread:
            return result
        
        # Compute inventory skew
        # Positive inventory -> skew DOWN (lower bids, lower asks)
        # Negative inventory -> skew UP (not applicable for no-short)
        inventory_ratio = inventory_shares / max_inventory if max_inventory > 0 else 0
        skew = inventory_ratio * self.params.inventory_skew_factor * (self.params.target_half_spread_cents / 100)
        
        # Compute base bid/ask around mid
        half_spread = self.params.target_half_spread_cents / 100
        
        base_bid = book.mid - half_spread
        base_ask = book.mid + half_spread
        
        # Apply skew
        bid_price = base_bid - skew
        ask_price = base_ask - skew
        
        # Round to tick
        bid_price = round_to_tick(bid_price, self.params.tick_size)
        ask_price = round_to_tick(ask_price, self.params.tick_size)
        
        # Clamp to valid range
        bid_price = clamp_price(bid_price, self.params.min_price, self.params.max_price)
        ask_price = clamp_price(ask_price, self.params.min_price, self.params.max_price)
        
        # CRITICAL: Ensure bid < ask
        if bid_price >= ask_price:
            # Widen spread
            bid_price = round_to_tick(book.mid - half_spread * 1.5, self.params.tick_size)
            ask_price = round_to_tick(book.mid + half_spread * 1.5, self.params.tick_size)
            bid_price = clamp_price(bid_price)
            ask_price = clamp_price(ask_price)
        
        # Final check
        if bid_price >= ask_price:
            return result  # Can't quote safely
        
        # Compute sizes
        bid_size = self.params.base_quote_size
        ask_size = self.params.base_quote_size
        
        # Adjust bid size based on USDC available
        max_bid_shares = usdc_available / bid_price if bid_price > 0 else 0
        bid_size = min(bid_size, max_bid_shares)
        
        # Adjust sizes based on inventory limits
        remaining_buy_capacity = max_inventory - inventory_shares
        bid_size = min(bid_size, remaining_buy_capacity)
        
        # Can't sell more than we have
        ask_size = min(ask_size, inventory_shares)
        
        # Create quotes if valid
        if bid_size > 0 and remaining_buy_capacity > 0:
            result.bid = Quote(
                price=bid_price,
                size=bid_size,
                side="BUY",
                skew_applied=skew
            )
        
        if ask_size > 0 and inventory_shares > 0:
            result.ask = Quote(
                price=ask_price,
                size=ask_size,
                side="SELL",
                skew_applied=skew
            )
        
        return result
    
    def validate_quote(self, quote: Quote, book: OrderBook) -> Tuple[bool, str]:
        """
        Validate a quote before posting.
        
        Returns:
            (is_valid, reason)
        """
        # Price bounds
        if quote.price < self.params.min_price or quote.price > self.params.max_price:
            return False, f"Price {quote.price} out of bounds"
        
        # Size must be positive
        if quote.size <= 0:
            return False, "Size must be positive"
        
        # For BUY: price must be below best ask (otherwise would cross)
        if quote.side == "BUY" and quote.price >= book.best_ask:
            return False, f"Buy price {quote.price} would cross ask {book.best_ask}"
        
        # For SELL: price must be above best bid (otherwise would cross)
        if quote.side == "SELL" and quote.price <= book.best_bid:
            return False, f"Sell price {quote.price} would cross bid {book.best_bid}"
        
        return True, ""
    
    def should_replace(
        self,
        current_price: float,
        new_price: float,
        min_change: float = 0.01
    ) -> bool:
        """Check if quote should be replaced"""
        return abs(current_price - new_price) >= min_change

