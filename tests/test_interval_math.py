"""
Tests for interval math and constant payout sizing.
"""

import pytest
import math
from datetime import date, datetime
from unittest.mock import Mock, patch

import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent))

from bot.model import TemperatureModel, BucketProbability, IntervalProbability
from bot.strategy_interval import IntervalStrategy, TradePlan, TradeLeg
from bot.clob import FillResult, OrderBook, OrderBookLevel
from bot.gamma import TemperatureMarket
from bot.weather import DailyForecast


class TestNormalDistributionModel:
    """Tests for the probability model using Normal distribution."""
    
    def test_bucket_probability_center(self):
        """Test probability of bucket centered on mean."""
        model = TemperatureModel()
        
        # Bucket exactly centered on mean should have highest probability
        mu, sigma = 50.0, 2.0
        prob_center = model.bucket_probability(mu, sigma, 49.5, 50.5)
        prob_edge = model.bucket_probability(mu, sigma, 55.0, 56.0)
        
        assert prob_center > prob_edge
    
    def test_bucket_probability_sums_to_one(self):
        """Test that bucket probabilities sum to ~1 over reasonable range."""
        model = TemperatureModel()
        mu, sigma = 50.0, 3.0
        
        # Sum probabilities from 35-65°F (10 sigma range)
        total_prob = 0
        for t in range(35, 65):
            total_prob += model.bucket_probability(mu, sigma, t, t + 1)
        
        # Should be very close to 1
        assert 0.99 < total_prob < 1.01
    
    def test_bucket_probability_symmetry(self):
        """Test that probabilities are symmetric around mean."""
        model = TemperatureModel()
        mu, sigma = 50.0, 2.0
        
        prob_above = model.bucket_probability(mu, sigma, 52.0, 53.0)
        prob_below = model.bucket_probability(mu, sigma, 47.0, 48.0)
        
        # Should be equal (within floating point tolerance)
        assert abs(prob_above - prob_below) < 0.001
    
    def test_interval_probability_wider_is_higher(self):
        """Test that wider intervals have higher probability."""
        model = TemperatureModel()
        mu, sigma = 50.0, 2.0
        
        prob_1f = model.interval_probability(mu, sigma, 49.5, 50.5)
        prob_3f = model.interval_probability(mu, sigma, 48.5, 51.5)
        prob_5f = model.interval_probability(mu, sigma, 47.5, 52.5)
        
        assert prob_1f < prob_3f < prob_5f
    
    def test_interval_probability_bounds(self):
        """Test probability bounds (0 <= p <= 1)."""
        model = TemperatureModel()
        mu, sigma = 50.0, 2.0
        
        prob = model.interval_probability(mu, sigma, 45.0, 55.0)
        
        assert 0 <= prob <= 1
    
    def test_interval_probability_full_range(self):
        """Test that very wide interval approaches 1."""
        model = TemperatureModel()
        mu, sigma = 50.0, 2.0
        
        prob = model.interval_probability(mu, sigma, 0.0, 100.0)
        
        assert prob > 0.999


class TestConstantPayoutSizing:
    """Tests for constant payout sizing logic."""
    
    def test_constant_payout_per_bucket(self):
        """
        Test that buying S shares per bucket yields constant payout.
        
        If we buy S shares of each bucket:
        - If bucket i wins: payout = S (since YES pays 1.00)
        - Cost = S * Σ(ask_i)
        
        The payout is the same regardless of WHICH bucket wins.
        """
        # Simulate 3 buckets with different ask prices
        asks = [0.20, 0.35, 0.25]  # Sum = 0.80
        shares_per_bucket = 10
        
        total_cost = shares_per_bucket * sum(asks)
        
        # Payout if bucket 0 wins
        payout_0 = shares_per_bucket * 1.0  # YES pays 1.00
        
        # Payout if bucket 1 wins
        payout_1 = shares_per_bucket * 1.0
        
        # Payout if bucket 2 wins
        payout_2 = shares_per_bucket * 1.0
        
        # All payouts are equal
        assert payout_0 == payout_1 == payout_2 == shares_per_bucket
    
    def test_profit_calculation(self):
        """Test profit calculation for interval hit/miss."""
        asks = [0.20, 0.35, 0.25]
        shares = 10
        
        total_cost = shares * sum(asks)  # 10 * 0.80 = 8.00
        payout_if_hit = shares * 1.0  # 10.00
        
        profit_if_hit = payout_if_hit - total_cost  # 10 - 8 = 2
        loss_if_miss = total_cost  # 8.00
        
        assert profit_if_hit == 2.0
        assert loss_if_miss == 8.0
    
    def test_sizing_for_risk_limit(self):
        """Test calculating shares for a given risk limit."""
        asks = [0.20, 0.35, 0.25]
        cost_per_unit = sum(asks)  # 0.80 per share across all buckets
        max_risk = 10.0  # Max $10 risk
        
        # S = max_risk / cost_per_unit
        max_shares = max_risk / cost_per_unit  # 10 / 0.80 = 12.5
        shares = int(max_shares)  # Floor to 12
        
        actual_cost = shares * cost_per_unit
        assert actual_cost <= max_risk
    
    def test_edge_calculation(self):
        """Test edge calculation: P_model - implied_cost."""
        # Model says 45% chance of interval hit
        p_model = 0.45
        
        # Market asks sum to 0.40 (implied 40% for complete set)
        implied_cost = 0.40
        
        edge = p_model - implied_cost
        
        assert abs(edge - 0.05) < 0.001  # 5% edge
    
    def test_expected_value(self):
        """Test expected value calculation."""
        p_model = 0.45
        asks = [0.15, 0.15, 0.10]  # Sum = 0.40
        shares = 10
        
        cost = shares * sum(asks)  # 4.00
        payout = shares  # 10.00
        
        # EV = P(hit) * profit_if_hit - P(miss) * loss_if_miss
        # profit_if_hit = payout - cost = 6.00
        # loss_if_miss = cost = 4.00
        ev = p_model * (payout - cost) - (1 - p_model) * cost
        
        expected_ev = 0.45 * 6 - 0.55 * 4  # 2.7 - 2.2 = 0.5
        assert abs(ev - expected_ev) < 0.001


class TestFillCostCalculation:
    """Tests for orderbook fill cost calculation."""
    
    def create_mock_orderbook(self, asks):
        """Create a mock orderbook with given asks."""
        levels = [OrderBookLevel(price=p, size=s) for p, s in asks]
        return OrderBook(token_id="test", bids=[], asks=levels)
    
    def test_single_level_fill(self):
        """Test fill at single price level."""
        # Enough depth at one level
        asks = [(0.30, 100)]  # 100 shares at 0.30
        
        book = self.create_mock_orderbook(asks)
        
        shares_needed = 50
        total_cost = 0
        remaining = shares_needed
        
        for level in book.asks:
            if remaining <= 0:
                break
            fill = min(remaining, level.size)
            total_cost += fill * level.price
            remaining -= fill
        
        avg_price = total_cost / shares_needed
        
        assert avg_price == 0.30
        assert total_cost == 15.0  # 50 * 0.30
    
    def test_multi_level_fill(self):
        """Test fill across multiple price levels."""
        asks = [(0.30, 30), (0.32, 50), (0.35, 100)]
        
        book = self.create_mock_orderbook(asks)
        
        shares_needed = 50
        total_cost = 0
        filled = 0
        
        for level in book.asks:
            if filled >= shares_needed:
                break
            fill = min(shares_needed - filled, level.size)
            total_cost += fill * level.price
            filled += fill
        
        avg_price = total_cost / shares_needed
        
        # 30 shares at 0.30 = 9.00
        # 20 shares at 0.32 = 6.40
        # Total: 15.40 for 50 shares = 0.308 avg
        expected_cost = 30 * 0.30 + 20 * 0.32
        assert abs(total_cost - expected_cost) < 0.001
        assert abs(avg_price - 0.308) < 0.001
    
    def test_insufficient_depth(self):
        """Test detection of insufficient depth."""
        asks = [(0.30, 20), (0.32, 20)]  # Only 40 shares available
        
        book = self.create_mock_orderbook(asks)
        
        shares_needed = 50
        filled = 0
        
        for level in book.asks:
            filled += level.size
        
        can_fill = filled >= shares_needed
        assert can_fill is False


class TestIntervalSelection:
    """Tests for interval selection logic."""
    
    def create_mock_markets(self, buckets):
        """Create mock TemperatureMarket objects."""
        markets = []
        for i, (tmin, tmax) in enumerate(buckets):
            markets.append(TemperatureMarket(
                market_id=f"market_{i}",
                question=f"Will temp be {tmin}-{tmax}°F?",
                slug=f"london-temp-{tmin}-{tmax}",
                yes_token_id=f"token_{i}",
                no_token_id=f"no_token_{i}",
                tmin_f=tmin,
                tmax_f=tmax,
                target_date=date.today(),
                location="London",
                enable_order_book=True
            ))
        return markets
    
    def test_interval_edge_ranking(self):
        """Test that intervals are ranked by edge."""
        # Simulate evaluating multiple intervals
        intervals = [
            {"tmin": 50, "tmax": 52, "p_model": 0.40, "implied": 0.35},  # edge = 0.05
            {"tmin": 52, "tmax": 54, "p_model": 0.35, "implied": 0.28},  # edge = 0.07
            {"tmin": 54, "tmax": 56, "p_model": 0.30, "implied": 0.32},  # edge = -0.02
        ]
        
        for i in intervals:
            i["edge"] = i["p_model"] - i["implied"]
        
        # Sort by edge descending
        best = max(intervals, key=lambda x: x["edge"])
        
        assert best["tmin"] == 52  # 52-54 has best edge
        assert abs(best["edge"] - 0.07) < 0.001
    
    def test_edge_buffer_filter(self):
        """Test that intervals below edge buffer are rejected."""
        edge_buffer = 0.02  # 2%
        
        intervals = [
            {"edge": 0.05},  # Above buffer
            {"edge": 0.015},  # Below buffer
            {"edge": 0.02},  # Exactly at buffer
        ]
        
        valid = [i for i in intervals if i["edge"] >= edge_buffer]
        
        assert len(valid) == 2
    
    def test_max_width_constraint(self):
        """Test that intervals exceeding max width are rejected."""
        max_width = 6  # 6°F max
        
        intervals = [
            {"tmin": 50, "tmax": 55},  # 5°F - OK
            {"tmin": 50, "tmax": 56},  # 6°F - OK
            {"tmin": 50, "tmax": 57},  # 7°F - Too wide
        ]
        
        for i in intervals:
            i["width"] = i["tmax"] - i["tmin"]
        
        valid = [i for i in intervals if i["width"] <= max_width]
        
        assert len(valid) == 2


class TestTradePlanConstruction:
    """Tests for TradePlan construction and properties."""
    
    def create_mock_trade_plan(self, legs_data, shares=10):
        """Create a mock TradePlan."""
        legs = []
        per_bucket_prices = []
        for tmin, tmax, price in legs_data:
            market = TemperatureMarket(
                market_id="test",
                question=f"Temp {tmin}-{tmax}",
                slug="test",
                yes_token_id=f"token_{tmin}",
                no_token_id=None,
                tmin_f=tmin,
                tmax_f=tmax,
                target_date=date.today(),
                location="London",
                enable_order_book=True
            )
            fill = FillResult(
                can_fill=True,
                total_shares=shares,
                total_cost=shares * price,
                avg_price=price,
                levels_used=1,
                remaining_shares=0
            )
            legs.append(TradeLeg(
                market=market,
                token_id=f"token_{tmin}",
                shares=shares,
                limit_price=price,
                fill_result=fill
            ))
            per_bucket_prices.append((tmin, tmax, price))
        
        total_cost = sum(leg.cost for leg in legs)
        implied = total_cost / shares
        
        return TradePlan(
            target_date=date.today(),
            location="London",
            interval_tmin=legs_data[0][0],
            interval_tmax=legs_data[-1][1],
            legs=legs,
            forecast_mu=52.0,
            forecast_sigma=2.0,
            sigma_raw=2.0,
            sigma_k=1.0,
            p_interval=0.45,
            p_interval_mc=0.45,
            mc_validated=True,
            per_bucket_prices=per_bucket_prices,
            implied_cost=implied,
            edge=0.45 - implied,
            shares_per_leg=shares,
            total_cost=total_cost,
            payout_if_hit=shares,
            max_loss=total_cost
        )
    
    def test_interval_string(self):
        """Test interval string formatting."""
        plan = self.create_mock_trade_plan([
            (50, 51, 0.20),
            (51, 52, 0.15),
        ])
        assert plan.interval_str == "50-52F"
    
    def test_num_buckets(self):
        """Test bucket count."""
        plan = self.create_mock_trade_plan([
            (50, 51, 0.20),
            (51, 52, 0.15),
            (52, 53, 0.10),
        ])
        assert plan.num_buckets == 3
    
    def test_expected_pnl(self):
        """Test expected P&L calculation."""
        plan = self.create_mock_trade_plan([
            (50, 51, 0.20),
            (51, 52, 0.15),
        ], shares=10)
        
        # Total cost = 10 * (0.20 + 0.15) = 3.50
        # Payout if hit = 10
        # Profit if hit = 10 - 3.50 = 6.50
        # p_interval = 0.45
        # EV = 0.45 * 6.50 - 0.55 * 3.50 = 2.925 - 1.925 = 1.00
        
        assert abs(plan.expected_pnl - 1.0) < 0.01
    
    def test_to_orders(self):
        """Test converting trade plan to orders."""
        plan = self.create_mock_trade_plan([
            (50, 51, 0.20),
            (51, 52, 0.15),
        ], shares=10)
        
        orders = plan.to_orders()
        
        assert len(orders) == 2
        for order in orders:
            assert order.shares == 10
            assert order.side.value == "BUY"


if __name__ == "__main__":
    pytest.main([__file__, "-v"])

