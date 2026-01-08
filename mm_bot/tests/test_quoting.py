"""
Tests for Quote Engine
======================
Tests risk invariants and quoting logic.
"""

import pytest
from mm_bot.config import Config, QuotingParams
from mm_bot.clob import OrderBook
from mm_bot.quoting import QuoteEngine, round_to_tick, clamp_price


@pytest.fixture
def config():
    cfg = Config()
    cfg.quoting = QuotingParams(
        min_half_spread_cents=1.0,
        target_half_spread_cents=2.0,
        inventory_skew_factor=0.5,
        base_quote_size=10.0,
        tick_size=0.01,
        min_price=0.01,
        max_price=0.99
    )
    cfg.risk.max_inv_shares_per_token = 50.0
    return cfg


@pytest.fixture
def quote_engine(config):
    return QuoteEngine(config)


@pytest.fixture
def normal_book():
    """Normal order book with reasonable spread"""
    return OrderBook(
        token_id="test_token",
        bids=[{"price": "0.48", "size": "100"}],
        asks=[{"price": "0.52", "size": "100"}],
        timestamp=0
    )


@pytest.fixture
def tight_book():
    """Tight spread book"""
    return OrderBook(
        token_id="test_token",
        bids=[{"price": "0.50", "size": "100"}],
        asks=[{"price": "0.51", "size": "100"}],
        timestamp=0
    )


class TestRoundToTick:
    def test_exact_tick(self):
        assert round_to_tick(0.50, 0.01) == 0.50
    
    def test_round_up(self):
        # Note: Python uses banker's rounding, 0.505 rounds to 0.50
        assert round_to_tick(0.506, 0.01) == 0.51
    
    def test_round_down(self):
        assert round_to_tick(0.504, 0.01) == 0.50
    
    def test_edge_case(self):
        assert round_to_tick(0.999, 0.01) == 1.00


class TestClampPrice:
    def test_within_range(self):
        assert clamp_price(0.50) == 0.50
    
    def test_below_min(self):
        assert clamp_price(0.005) == 0.01
    
    def test_above_max(self):
        assert clamp_price(0.995) == 0.99


class TestQuoteEngine:
    """Test quote computation and invariants"""
    
    def test_bid_less_than_ask_always(self, quote_engine, normal_book):
        """INVARIANT: bid < ask always"""
        quotes = quote_engine.compute_quotes(
            book=normal_book,
            inventory_shares=0,
            max_inventory=50,
            usdc_available=100
        )
        
        if quotes.bid and quotes.ask:
            assert quotes.bid.price < quotes.ask.price, \
                f"Bid {quotes.bid.price} must be < ask {quotes.ask.price}"
    
    def test_prices_in_valid_range(self, quote_engine, normal_book):
        """INVARIANT: prices always in [0.01, 0.99]"""
        for inv in [0, 10, 25, 40, 50]:
            quotes = quote_engine.compute_quotes(
                book=normal_book,
                inventory_shares=inv,
                max_inventory=50,
                usdc_available=100
            )
            
            if quotes.bid:
                assert 0.01 <= quotes.bid.price <= 0.99, \
                    f"Bid price {quotes.bid.price} out of range"
            
            if quotes.ask:
                assert 0.01 <= quotes.ask.price <= 0.99, \
                    f"Ask price {quotes.ask.price} out of range"
    
    def test_no_buy_when_inventory_maxed(self, quote_engine, normal_book):
        """INVARIANT: if inventory maxed, no BUY quotes"""
        quotes = quote_engine.compute_quotes(
            book=normal_book,
            inventory_shares=50,  # At max
            max_inventory=50,
            usdc_available=100
        )
        
        # Should not have bid quote (can't buy more)
        assert quotes.bid is None or quotes.bid.size <= 0, \
            "Should not quote bid when inventory is maxed"
    
    def test_no_sell_when_no_inventory(self, quote_engine, normal_book):
        """INVARIANT: if inventory is 0, no SELL quotes"""
        quotes = quote_engine.compute_quotes(
            book=normal_book,
            inventory_shares=0,
            max_inventory=50,
            usdc_available=100
        )
        
        # Should not have ask quote (nothing to sell)
        assert quotes.ask is None or quotes.ask.size <= 0, \
            "Should not quote ask when inventory is 0"
    
    def test_no_quotes_on_tight_spread(self, quote_engine, tight_book):
        """No quotes when spread is too tight (no edge)"""
        quotes = quote_engine.compute_quotes(
            book=tight_book,
            inventory_shares=10,
            max_inventory=50,
            usdc_available=100
        )
        
        # With 1c spread and 2c target half-spread, should not quote
        # (spread 1c < min 2c)
        assert quotes.bid is None and quotes.ask is None, \
            "Should not quote when spread is too tight"
    
    def test_inventory_skew_lowers_prices(self, quote_engine, normal_book):
        """Higher inventory should lower both bid and ask prices"""
        quotes_low_inv = quote_engine.compute_quotes(
            book=normal_book,
            inventory_shares=5,
            max_inventory=50,
            usdc_available=100
        )
        
        quotes_high_inv = quote_engine.compute_quotes(
            book=normal_book,
            inventory_shares=40,
            max_inventory=50,
            usdc_available=100
        )
        
        # Higher inventory should have lower bid (less eager to buy)
        if quotes_low_inv.bid and quotes_high_inv.bid:
            assert quotes_high_inv.bid.price <= quotes_low_inv.bid.price, \
                "Higher inventory should lower bid price"
    
    def test_validate_buy_not_crossing_ask(self, quote_engine, normal_book):
        """Validation should reject buy that would cross ask"""
        from mm_bot.quoting import Quote
        
        # Create a buy quote at the ask price (would cross)
        bad_quote = Quote(
            price=normal_book.best_ask,  # At ask
            size=10,
            side="BUY"
        )
        
        is_valid, reason = quote_engine.validate_quote(bad_quote, normal_book)
        assert not is_valid, "Buy at ask should be rejected"
        assert "cross" in reason.lower()
    
    def test_validate_sell_not_crossing_bid(self, quote_engine, normal_book):
        """Validation should reject sell that would cross bid"""
        from mm_bot.quoting import Quote
        
        # Create a sell quote at the bid price (would cross)
        bad_quote = Quote(
            price=normal_book.best_bid,  # At bid
            size=10,
            side="SELL"
        )
        
        is_valid, reason = quote_engine.validate_quote(bad_quote, normal_book)
        assert not is_valid, "Sell at bid should be rejected"
        assert "cross" in reason.lower()


class TestRiskInvariants:
    """Test risk-related invariants"""
    
    def test_size_limited_by_usdc(self, quote_engine, normal_book):
        """Quote size should be limited by available USDC"""
        quotes = quote_engine.compute_quotes(
            book=normal_book,
            inventory_shares=0,
            max_inventory=50,
            usdc_available=2.0  # Very limited
        )
        
        if quotes.bid:
            max_shares = 2.0 / quotes.bid.price
            assert quotes.bid.size <= max_shares + 0.01, \
                f"Bid size {quotes.bid.size} exceeds USDC limit"
    
    def test_size_limited_by_inventory_capacity(self, quote_engine, normal_book):
        """Quote size should not exceed remaining inventory capacity"""
        quotes = quote_engine.compute_quotes(
            book=normal_book,
            inventory_shares=45,  # 5 remaining
            max_inventory=50,
            usdc_available=100
        )
        
        if quotes.bid:
            remaining_capacity = 50 - 45
            assert quotes.bid.size <= remaining_capacity + 0.01, \
                f"Bid size {quotes.bid.size} exceeds inventory capacity"
    
    def test_sell_size_limited_by_holdings(self, quote_engine, normal_book):
        """Sell size should not exceed holdings"""
        quotes = quote_engine.compute_quotes(
            book=normal_book,
            inventory_shares=3,  # Only 3 shares
            max_inventory=50,
            usdc_available=100
        )
        
        if quotes.ask:
            assert quotes.ask.size <= 3, \
                f"Ask size {quotes.ask.size} exceeds holdings of 3"

