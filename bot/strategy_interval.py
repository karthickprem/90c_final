"""
Interval selection strategy.
Finds the best contiguous interval of buckets to trade.
Uses depth-walked executable prices and hard filters.
"""

import logging
from typing import List, Dict, Optional, Tuple
from datetime import date
from dataclasses import dataclass, field
from enum import Enum

from bot.gamma import TemperatureMarket, group_markets_by_date
from bot.clob import CLOBClient, FillResult, Side, Order
from bot.model import TemperatureModel, IntervalProbability
from bot.weather import DailyForecast
from bot.resolution import validate_bucket_lattice, sanity_check_lattice_prices

logger = logging.getLogger(__name__)


class SkipReason(Enum):
    """Reasons for skipping a trade opportunity."""
    NO_MARKETS = "no_markets_found"
    NO_FORECAST = "no_forecast_available"
    INVALID_LATTICE = "invalid_bucket_lattice"
    INSANE_PRICES = "price_sum_far_from_1"
    EDGE_TOO_LOW = "edge_below_minimum"
    DEPTH_INSUFFICIENT = "insufficient_orderbook_depth"
    COST_EXCEEDS_RISK = "cost_exceeds_risk_limit"
    SHARES_TOO_SMALL = "shares_less_than_minimum"
    MC_VALIDATION_FAILED = "monte_carlo_validation_failed"
    INTERVAL_TOO_WIDE = "interval_exceeds_max_width"
    NO_CONTIGUOUS_INTERVAL = "no_contiguous_interval_found"
    SAME_DAY_MARKET = "same_day_market_outcome_may_be_known"


@dataclass
class TradeLeg:
    """Single leg of an interval trade (one bucket)."""
    market: TemperatureMarket
    token_id: str
    shares: float
    limit_price: float  # Avg fill price from depth walk
    fill_result: FillResult
    
    @property
    def cost(self) -> float:
        return self.fill_result.total_cost
    
    @property
    def can_execute(self) -> bool:
        return self.fill_result.can_fill


@dataclass
class TradePlan:
    """Complete plan for trading an interval."""
    target_date: date
    location: str
    interval_tmin: float
    interval_tmax: float
    legs: List[TradeLeg]
    
    # Model predictions with calibration info
    forecast_mu: float
    forecast_sigma: float
    sigma_raw: float
    sigma_k: float
    p_interval: float  # Model probability
    p_interval_mc: float  # Monte Carlo validated probability
    mc_validated: bool
    
    # Market pricing (from depth walk)
    per_bucket_prices: List[Tuple[float, float, float]]  # (tmin, tmax, avg_fill_price)
    implied_cost: float  # Sum of depth-walked fill prices per share
    edge: float  # p_interval - implied_cost
    
    # Sizing
    shares_per_leg: float  # S (constant payout sizing)
    total_cost: float  # Actual cost to execute
    payout_if_hit: float  # S (one bucket wins -> we get S)
    max_loss: float  # total_cost (if interval misses)
    
    # Execution readiness
    all_legs_fillable: bool = True
    skip_reason: Optional[SkipReason] = None
    skip_detail: str = ""
    
    @property
    def interval_str(self) -> str:
        # Show one decimal if Celsius-derived (widths around 1.8F), integer if Fahrenheit
        width = self.interval_tmax - self.interval_tmin
        if abs(width - round(width)) > 0.1:  # Non-integer width
            return f"{self.interval_tmin:.1f}-{self.interval_tmax:.1f}F"
        return f"{self.interval_tmin:.0f}-{self.interval_tmax:.0f}F"
    
    @property
    def num_buckets(self) -> int:
        return len(self.legs)
    
    @property
    def expected_pnl(self) -> float:
        """Expected P&L = P_hit * profit_if_hit + P_miss * loss_if_miss"""
        profit_if_hit = self.payout_if_hit - self.total_cost
        return self.p_interval * profit_if_hit - (1 - self.p_interval) * self.total_cost
    
    def to_orders(self, slippage_cap: float = 0.005) -> List[Order]:
        """Convert trade plan to list of orders."""
        orders = []
        for leg in self.legs:
            # Add small slippage buffer to limit price
            limit_price = min(1.0, leg.limit_price * (1 + slippage_cap))
            orders.append(Order(
                token_id=leg.token_id,
                side=Side.BUY,
                shares=leg.shares,
                limit_price=limit_price
            ))
        return orders
    
    def print_per_bucket_prices(self):
        """Print per-bucket fill prices for transparency."""
        print(f"\n  Per-bucket executable prices (depth-walked for {self.shares_per_leg:.0f} shares):")
        for tmin, tmax, price in self.per_bucket_prices:
            width = tmax - tmin
            if abs(width - round(width)) > 0.1:  # Non-integer width (Celsius)
                print(f"    {tmin:.1f}-{tmax:.1f}F: {price:.4f}")
            else:
                print(f"    {tmin:.0f}-{tmax:.0f}F: {price:.4f}")
        print(f"  Total implied (sum): {self.implied_cost:.4f}")


@dataclass
class SkippedOpportunity:
    """Record of a skipped opportunity with reason."""
    target_date: date
    location: str
    reason: SkipReason
    detail: str
    interval_tmin: Optional[float] = None
    interval_tmax: Optional[float] = None
    edge: Optional[float] = None


class IntervalStrategy:
    """
    Strategy for finding and sizing interval trades.
    
    Key improvements:
    - Uses depth-walked prices for accurate edge calculation
    - Hard filters to avoid bad trades
    - Tracks skip reasons for debugging
    """
    
    # Minimum edge to enter (higher than before)
    MIN_EDGE_DEFAULT = 0.03  # 3% minimum
    MIN_EDGE_CONSERVATIVE = 0.05  # 5% for thin markets
    
    # Minimum shares per leg
    MIN_SHARES = 5
    
    def __init__(self, config: dict):
        self.config = config
        self.clob = CLOBClient(config=config)
        self.model = TemperatureModel(config=config)
        
        # Strategy parameters
        self.min_edge = config.get("min_edge", self.MIN_EDGE_DEFAULT)
        self.edge_buffer = config.get("edge_buffer", 0.02)  # Additional buffer
        self.max_risk_per_day = config.get("max_risk_per_day_usd", 10)
        self.max_total_risk = config.get("max_total_open_risk_usd", 30)
        self.max_interval_width = config.get("interval_max_width_f", 6)
        self.min_liquidity = config.get("min_liquidity_shares", 50)
        self.use_depth = config.get("use_depth", True)
        self.price_sum_tolerance = config.get("price_sum_tolerance", 0.20)
        
        # Track skipped opportunities
        self.skipped: List[SkippedOpportunity] = []
    
    def _skip(self, target_date: date, location: str, reason: SkipReason, 
              detail: str, interval: Tuple[float, float] = None, 
              edge: float = None) -> None:
        """Record a skipped opportunity."""
        self.skipped.append(SkippedOpportunity(
            target_date=target_date,
            location=location,
            reason=reason,
            detail=detail,
            interval_tmin=interval[0] if interval else None,
            interval_tmax=interval[1] if interval else None,
            edge=edge
        ))
        logger.info(f"SKIP: {reason.value} - {detail}")
    
    def _get_bucket_prices_with_depth(self, markets: List[TemperatureMarket], 
                                       target_shares: float) -> Dict[str, Tuple[float, FillResult, TemperatureMarket]]:
        """
        Get executable prices for each market by walking orderbook depth.
        
        Returns dict mapping token_id to (avg_price, FillResult, market)
        """
        prices = {}
        
        for market in markets:
            token_id = market.yes_token_id
            
            # Always walk the orderbook for accurate pricing
            fill = self.clob.fill_cost_for_shares(token_id, target_shares)
            
            if fill.can_fill and fill.avg_price > 0:
                prices[token_id] = (fill.avg_price, fill, market)
            else:
                logger.debug(f"No fill for {market.tmin_f}-{market.tmax_f}F: {fill}")
        
        return prices
    
    def _validate_lattice(self, markets: List[TemperatureMarket], 
                          target_date: date, location: str) -> bool:
        """Validate that buckets form a proper lattice (soft validation)."""
        buckets = [(m.tmin_f, m.tmax_f) for m in markets]
        validation = validate_bucket_lattice(buckets)
        
        # Soft validation - only reject if coverage is terrible
        if not validation.is_valid and validation.coverage < 0.5:
            msg = "; ".join(validation.issues[:3])
            self._skip(target_date, location, SkipReason.INVALID_LATTICE, msg)
            return False
        
        # Validate bucket width consistency
        from bot.parse_buckets import validate_bucket_group_consistency
        is_consistent, mode_width, issues = validate_bucket_group_consistency(buckets)
        
        if not is_consistent:
            # Log warning but don't reject - the intervals we build will respect widths
            logger.warning(f"Bucket widths inconsistent for {location} {target_date}: {issues[:2]}")
        
        return True
    
    def _sanity_check_prices(self, markets: List[TemperatureMarket],
                              prices: Dict[str, Tuple[float, FillResult, TemperatureMarket]],
                              target_date: date, location: str) -> bool:
        """Check that prices sum to approximately 1.0."""
        if len(prices) != len(markets):
            self._skip(target_date, location, SkipReason.DEPTH_INSUFFICIENT,
                      f"Only {len(prices)}/{len(markets)} buckets have depth")
            return False
        
        buckets = [(m.tmin_f, m.tmax_f) for m in markets]
        price_list = [prices[m.yes_token_id][0] for m in markets]
        
        sane, msg, total = sanity_check_lattice_prices(buckets, price_list, self.price_sum_tolerance)
        
        if not sane:
            self._skip(target_date, location, SkipReason.INSANE_PRICES,
                      f"Price sum {total:.3f} far from 1.0")
            return False
        
        return True
    
    def _build_intervals(self, markets: List[TemperatureMarket]) -> List[List[TemperatureMarket]]:
        """
        Build all contiguous intervals from sorted markets.
        Returns list of intervals, each interval is a list of markets.
        """
        if not markets:
            return []
        
        # Sort by tmin
        sorted_markets = sorted(markets, key=lambda m: m.tmin_f)
        
        intervals = []
        n = len(sorted_markets)
        
        for start in range(n):
            interval = [sorted_markets[start]]
            interval_width = sorted_markets[start].bucket_width
            
            # Single bucket is a valid interval
            if interval_width <= self.max_interval_width:
                intervals.append(list(interval))
            
            # Try to extend
            for end in range(start + 1, n):
                prev = sorted_markets[end - 1]
                curr = sorted_markets[end]
                
                # Check contiguity (prev.tmax should equal curr.tmin)
                if abs(prev.tmax_f - curr.tmin_f) > 0.01:
                    break
                
                interval.append(curr)
                interval_width = curr.tmax_f - sorted_markets[start].tmin_f
                
                if interval_width > self.max_interval_width:
                    break
                
                intervals.append(list(interval))
        
        return intervals
    
    def _calculate_shares_for_risk(self, cost_per_unit: float) -> float:
        """
        Calculate number of shares (S) to buy per bucket for constant payout sizing.
        """
        if cost_per_unit <= 0:
            return 0
        
        max_shares = self.max_risk_per_day / cost_per_unit
        return max(0, max_shares)
    
    def find_best_interval(self, markets: List[TemperatureMarket],
                            forecast: DailyForecast,
                            location: str) -> Optional[TradePlan]:
        """
        Find the best interval to trade for a given date.
        """
        if not markets or not forecast:
            return None
        
        target_date = forecast.target_date
        mu = forecast.high_temp_f
        sigma = forecast.uncertainty_sigma_f
        
        # CRITICAL: Don't trade same-day markets (outcome may be known)
        days_ahead = (target_date - date.today()).days
        if days_ahead <= 0:
            self._skip(target_date, location, SkipReason.SAME_DAY_MARKET,
                      f"Target date is today or past ({days_ahead} days ahead)")
            return None
        
        # Validate lattice
        if not self._validate_lattice(markets, target_date, location):
            return None
        
        # Get sigma calibration info
        days_ahead = (target_date - date.today()).days
        sigma_raw, sigma_k, sigma_used = self.model.get_sigma(max(0, days_ahead))
        
        # Build all candidate intervals
        intervals = self._build_intervals(markets)
        if not intervals:
            self._skip(target_date, location, SkipReason.NO_CONTIGUOUS_INTERVAL,
                      "No contiguous intervals found")
            return None
        
        best_plan: Optional[TradePlan] = None
        best_edge = -float('inf')
        
        for interval_markets in intervals:
            interval_tmin = interval_markets[0].tmin_f
            interval_tmax = interval_markets[-1].tmax_f
            
            # Get interval probability with MC validation
            interval_prob = self.model.get_interval_probability(
                forecast, interval_tmin, interval_tmax,
                buckets=[(m.tmin_f, m.tmax_f) for m in interval_markets],
                validate_mc=True
            )
            
            p_interval = interval_prob.probability
            p_mc = interval_prob.mc_probability
            mc_valid = interval_prob.mc_validated
            
            # First pass: estimate with 1 share to get cost per unit
            prices_unit = self._get_bucket_prices_with_depth(interval_markets, target_shares=1.0)
            
            if len(prices_unit) != len(interval_markets):
                continue  # Skip intervals with missing price data
            
            # Sum of ask prices (cost per unit payout)
            cost_per_unit = sum(p[0] for p in prices_unit.values())
            
            # Calculate edge
            edge = p_interval - cost_per_unit
            
            # Hard filter: minimum edge
            min_required_edge = self.min_edge + self.edge_buffer
            if edge < min_required_edge:
                self._skip(target_date, location, SkipReason.EDGE_TOO_LOW,
                          f"Edge {edge:.4f} < {min_required_edge:.4f}",
                          interval=(interval_tmin, interval_tmax), edge=edge)
                continue
            
            # Calculate optimal share size
            shares_per_leg = self._calculate_shares_for_risk(cost_per_unit)
            
            if shares_per_leg < self.MIN_SHARES:
                self._skip(target_date, location, SkipReason.SHARES_TOO_SMALL,
                          f"Shares {shares_per_leg:.1f} < minimum {self.MIN_SHARES}",
                          interval=(interval_tmin, interval_tmax), edge=edge)
                continue
            
            # Round down to integer shares
            shares_per_leg = int(shares_per_leg)
            
            # Second pass: get actual fill costs for the sized trade
            prices_sized = self._get_bucket_prices_with_depth(interval_markets, target_shares=shares_per_leg)
            
            if len(prices_sized) != len(interval_markets):
                self._skip(target_date, location, SkipReason.DEPTH_INSUFFICIENT,
                          f"Only {len(prices_sized)}/{len(interval_markets)} buckets fillable at {shares_per_leg} shares",
                          interval=(interval_tmin, interval_tmax), edge=edge)
                continue
            
            # Build trade legs and compute actual costs
            legs = []
            per_bucket_prices = []
            all_fillable = True
            total_cost = 0
            
            for market in interval_markets:
                token_id = market.yes_token_id
                avg_price, fill_result, _ = prices_sized[token_id]
                
                if not fill_result.can_fill:
                    all_fillable = False
                    break
                
                legs.append(TradeLeg(
                    market=market,
                    token_id=token_id,
                    shares=shares_per_leg,
                    limit_price=avg_price,
                    fill_result=fill_result
                ))
                
                per_bucket_prices.append((market.tmin_f, market.tmax_f, avg_price))
                total_cost += fill_result.total_cost
            
            if not all_fillable or len(legs) != len(interval_markets):
                continue
            
            # Recalculate implied cost with actual fills
            implied_cost = total_cost / shares_per_leg
            actual_edge = p_interval - implied_cost
            
            # Final edge check with actual depth-walked prices
            if actual_edge < min_required_edge:
                self._skip(target_date, location, SkipReason.EDGE_TOO_LOW,
                          f"Actual edge {actual_edge:.4f} < {min_required_edge:.4f} after depth walk",
                          interval=(interval_tmin, interval_tmax), edge=actual_edge)
                continue
            
            # Check total cost is within risk limit
            if total_cost > self.max_risk_per_day:
                self._skip(target_date, location, SkipReason.COST_EXCEEDS_RISK,
                          f"Cost ${total_cost:.2f} > limit ${self.max_risk_per_day}",
                          interval=(interval_tmin, interval_tmax), edge=actual_edge)
                continue
            
            # MC validation warning (not a hard filter, but logged)
            if not mc_valid:
                logger.warning(f"MC validation failed for {interval_tmin}-{interval_tmax}: "
                              f"CDF={p_interval:.4f}, MC={p_mc:.4f}")
            
            # This interval passes all checks
            if actual_edge > best_edge:
                best_edge = actual_edge
                best_plan = TradePlan(
                    target_date=target_date,
                    location=location,
                    interval_tmin=interval_tmin,
                    interval_tmax=interval_tmax,
                    legs=legs,
                    forecast_mu=mu,
                    forecast_sigma=sigma,
                    sigma_raw=sigma_raw,
                    sigma_k=sigma_k,
                    p_interval=p_interval,
                    p_interval_mc=p_mc,
                    mc_validated=mc_valid,
                    per_bucket_prices=per_bucket_prices,
                    implied_cost=implied_cost,
                    edge=actual_edge,
                    shares_per_leg=shares_per_leg,
                    total_cost=total_cost,
                    payout_if_hit=shares_per_leg,
                    max_loss=total_cost,
                    all_legs_fillable=True
                )
        
        return best_plan
    
    def scan_all_dates(self, markets: List[TemperatureMarket],
                        location: str) -> List[TradePlan]:
        """
        Scan all dates and find the best interval for each.
        """
        # Clear previous skipped records
        self.skipped = []
        
        by_date = group_markets_by_date(markets)
        plans = []
        
        for target_date, date_markets in sorted(by_date.items()):
            logger.info(f"Scanning {target_date}: {len(date_markets)} buckets")
            
            # Get forecast for this date
            forecast = self.model.get_forecast(location, target_date)
            if not forecast:
                self._skip(target_date, location, SkipReason.NO_FORECAST,
                          "Weather API returned no forecast")
                continue
            
            # Find best interval
            plan = self.find_best_interval(date_markets, forecast, location)
            if plan:
                plans.append(plan)
                logger.info(f"  OPPORTUNITY: {plan.interval_str} edge={plan.edge:.4f}")
            else:
                logger.info(f"  No opportunity (see skip reasons)")
        
        return plans
    
    def print_skip_summary(self):
        """Print summary of skipped opportunities."""
        if not self.skipped:
            print("\nNo opportunities were skipped.")
            return
        
        print(f"\n=== SKIPPED OPPORTUNITIES ({len(self.skipped)}) ===")
        
        # Group by reason
        by_reason: Dict[SkipReason, List[SkippedOpportunity]] = {}
        for skip in self.skipped:
            if skip.reason not in by_reason:
                by_reason[skip.reason] = []
            by_reason[skip.reason].append(skip)
        
        for reason, skips in sorted(by_reason.items(), key=lambda x: -len(x[1])):
            print(f"\n{reason.value}: {len(skips)}")
            for skip in skips[:3]:  # Show first 3 examples
                interval_str = f"[{skip.interval_tmin:.0f}-{skip.interval_tmax:.0f}]" if skip.interval_tmin else ""
                edge_str = f"edge={skip.edge:.4f}" if skip.edge else ""
                print(f"  {skip.target_date} {skip.location} {interval_str} {edge_str}")
                print(f"    -> {skip.detail}")


def print_trade_plan(plan: TradePlan):
    """Pretty print a trade plan with full pricing transparency."""
    print(f"\n{'='*60}")
    print(f"TRADE PLAN: {plan.interval_str} on {plan.target_date} ({plan.location})")
    print(f"{'='*60}")
    
    print(f"\nForecast:")
    print(f"  mu = {plan.forecast_mu:.1f}F")
    print(f"  sigma_raw = {plan.sigma_raw:.2f}F, k = {plan.sigma_k:.3f}")
    print(f"  sigma_used = {plan.forecast_sigma:.2f}F")
    
    print(f"\nProbability:")
    print(f"  P_model (CDF): {plan.p_interval:.4f} ({plan.p_interval*100:.2f}%)")
    print(f"  P_model (MC):  {plan.p_interval_mc:.4f} ({plan.p_interval_mc*100:.2f}%)")
    print(f"  MC validated: {'YES' if plan.mc_validated else 'NO (warning)'}")
    
    print(f"\nMarket Pricing (depth-walked):")
    plan.print_per_bucket_prices()
    
    print(f"\nEdge Analysis:")
    print(f"  Implied cost: {plan.implied_cost:.4f} ({plan.implied_cost*100:.2f}%)")
    print(f"  Edge: {plan.edge:.4f} ({plan.edge*100:.2f}%)")
    
    print(f"\nSizing:")
    print(f"  Shares per leg: {plan.shares_per_leg:.0f}")
    print(f"  Total cost: ${plan.total_cost:.2f}")
    print(f"  Payout if hit: ${plan.payout_if_hit:.2f}")
    print(f"  Max loss: ${plan.max_loss:.2f}")
    print(f"  Expected P&L: ${plan.expected_pnl:.2f}")
    
    print(f"\nLegs ({plan.num_buckets} buckets):")
    for leg in plan.legs:
        m = leg.market
        print(f"  BUY {leg.shares:.0f} YES {m.tmin_f:.0f}-{m.tmax_f:.0f}F @ {leg.limit_price:.4f}")
    print()


if __name__ == "__main__":
    # Test strategy
    import yaml
    logging.basicConfig(level=logging.INFO)
    
    with open("bot/config.yaml") as f:
        config = yaml.safe_load(f)
    
    from bot.gamma import GammaClient
    
    print("Discovering temperature markets...")
    gamma = GammaClient(config=config)
    markets = gamma.discover_bucket_markets(
        locations=["london", "new york"],
        debug=True
    )
    
    print(f"Found {len(markets)} bucket markets")
    
    if markets:
        strategy = IntervalStrategy(config)
        plans = strategy.scan_all_dates(markets, location="London")
        
        if plans:
            print(f"\nFound {len(plans)} opportunities:")
            for plan in plans:
                print_trade_plan(plan)
        else:
            print("\nNo trading opportunities found")
        
        # Show skip summary
        strategy.print_skip_summary()
