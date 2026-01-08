#!/usr/bin/env python3
"""
run_backtest.py - Backtest the strategy using historical data

Uses historical weather data to validate the model and strategy logic.
Since we don't have historical orderbook data, we simulate market prices.

Usage:
    python run_backtest.py [--days 365] [--start-date 2024-01-01]
"""

import argparse
import logging
import sys
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import List, Dict, Optional, Tuple
from dataclasses import dataclass
import random

import yaml
import numpy as np

# Add bot directory to path
sys.path.insert(0, str(Path(__file__).parent))

from bot.model import TemperatureModel
from bot.weather import DailyForecast, CITY_COORDINATES


def setup_logging(verbose: bool = False):
    """Configure logging."""
    level = logging.DEBUG if verbose else logging.INFO
    logging.basicConfig(
        level=level,
        format='%(asctime)s | %(levelname)-8s | %(message)s',
        datefmt='%Y-%m-%d %H:%M:%S'
    )


def load_config(config_path: str = "bot/config.yaml") -> dict:
    """Load configuration from YAML file."""
    with open(config_path) as f:
        return yaml.safe_load(f)


@dataclass
class SimulatedBucket:
    """A simulated bucket market."""
    tmin_f: float
    tmax_f: float
    true_prob: float  # Based on historical distribution
    market_ask: float  # Simulated market price


@dataclass
class BacktestDay:
    """Results for a single backtested day."""
    target_date: date
    actual_high_f: float
    forecast_mu: float
    forecast_sigma: float
    chosen_interval: Optional[Tuple[float, float]]
    interval_p_model: float
    interval_implied: float
    edge: float
    cost: float
    payout: float
    hit: bool
    pnl: float


class HistoricalWeatherProvider:
    """
    Provides historical weather data for backtesting.
    Uses Meteostat if available, otherwise generates synthetic data.
    """
    
    def __init__(self, location: str = "London"):
        self.location = location
        self._meteostat_available = False
        self._historical_data: Dict[date, float] = {}
        
        try:
            from meteostat import Point, Daily
            import pandas as pd
            self._meteostat_available = True
            self.Point = Point
            self.Daily = Daily
            self.pd = pd
        except ImportError:
            logging.warning("Meteostat not available. Using synthetic historical data.")
    
    def _c_to_f(self, celsius: float) -> float:
        return celsius * 9 / 5 + 32
    
    def load_historical(self, start_date: date, end_date: date) -> Dict[date, float]:
        """Load historical daily highs. Returns dict mapping date -> high temp (°F)."""
        if self._meteostat_available:
            return self._load_from_meteostat(start_date, end_date)
        else:
            return self._generate_synthetic(start_date, end_date)
    
    def _load_from_meteostat(self, start_date: date, end_date: date) -> Dict[date, float]:
        """Load from Meteostat API."""
        lat, lon = CITY_COORDINATES.get(self.location.lower(), (51.5074, -0.1278))
        
        try:
            point = self.Point(lat, lon)
            data = self.Daily(point, start_date, end_date)
            data = data.fetch()
            
            result = {}
            for idx, row in data.iterrows():
                if 'tmax' in row and not self.pd.isna(row['tmax']):
                    result[idx.date()] = self._c_to_f(row['tmax'])
            
            logging.info(f"Loaded {len(result)} days of historical data from Meteostat")
            return result
            
        except Exception as e:
            logging.error(f"Meteostat error: {e}")
            return self._generate_synthetic(start_date, end_date)
    
    def _generate_synthetic(self, start_date: date, end_date: date) -> Dict[date, float]:
        """
        Generate synthetic historical data based on London climate.
        Uses a simple seasonal model with daily noise.
        """
        result = {}
        current = start_date
        
        while current <= end_date:
            # Day of year (0-365)
            doy = current.timetuple().tm_yday
            
            # London average high temperatures by month (approximate):
            # Jan: 46°F, Apr: 56°F, Jul: 73°F, Oct: 58°F
            # Model as sinusoidal with annual cycle
            # Mean high around 55°F, amplitude around 14°F
            seasonal_mean = 55 + 14 * np.sin(2 * np.pi * (doy - 100) / 365)
            
            # Add daily noise (σ ≈ 5°F)
            daily_noise = np.random.normal(0, 5)
            
            high_temp = seasonal_mean + daily_noise
            result[current] = round(high_temp, 1)
            
            current += timedelta(days=1)
        
        logging.info(f"Generated {len(result)} days of synthetic historical data")
        return result


class MarketSimulator:
    """
    Simulates market prices for buckets.
    
    Creates a distribution of bucket prices that's related to but
    different from the true probability distribution.
    """
    
    def __init__(self, efficiency: float = 0.9, spread: float = 0.02):
        """
        Args:
            efficiency: How efficient the market is (0-1). Higher = closer to true probs.
            spread: Bid-ask spread to add.
        """
        self.efficiency = efficiency
        self.spread = spread
    
    def generate_bucket_prices(self, actual_high: float, 
                                bucket_range: Tuple[float, float] = (40, 80),
                                bucket_width: float = 1.0) -> List[SimulatedBucket]:
        """
        Generate simulated bucket prices for a day.
        
        Uses the actual high to determine "true" probabilities,
        then adds market noise/inefficiency.
        """
        buckets = []
        
        tmin = bucket_range[0]
        while tmin < bucket_range[1]:
            tmax = tmin + bucket_width
            
            # True probability based on actual outcome
            # Use a narrow distribution centered on actual high
            true_sigma = 2.0  # How concentrated around actual
            from scipy import stats
            dist = stats.norm(loc=actual_high, scale=true_sigma)
            true_prob = dist.cdf(tmax) - dist.cdf(tmin)
            
            # Market price = true_prob + noise + spread
            noise = np.random.normal(0, (1 - self.efficiency) * 0.1)
            market_ask = true_prob + noise + self.spread / 2
            market_ask = max(0.01, min(0.99, market_ask))  # Clip to valid range
            
            buckets.append(SimulatedBucket(
                tmin_f=tmin,
                tmax_f=tmax,
                true_prob=true_prob,
                market_ask=market_ask
            ))
            
            tmin = tmax
        
        return buckets


class BacktestEngine:
    """
    Runs backtests of the trading strategy.
    """
    
    def __init__(self, config: dict):
        self.config = config
        self.model = TemperatureModel(config=config)
        self.weather = HistoricalWeatherProvider(config.get("location", "London"))
        self.market_sim = MarketSimulator(efficiency=0.85)
        
        self.edge_buffer = config.get("edge_buffer", 0.02)
        self.max_interval_width = config.get("interval_max_width_f", 6)
        self.max_risk = config.get("max_risk_per_day_usd", 10)
    
    def _find_best_interval(self, buckets: List[SimulatedBucket], 
                             forecast: DailyForecast) -> Optional[Tuple[List[SimulatedBucket], float, float]]:
        """
        Find the best interval to trade.
        Returns (interval_buckets, p_model, implied_cost) or None.
        """
        mu = forecast.high_temp_f
        sigma = forecast.uncertainty_sigma_f
        
        n = len(buckets)
        best_interval = None
        best_edge = -float('inf')
        best_p = 0
        best_implied = 0
        
        # Try all contiguous intervals up to max width
        for start in range(n):
            interval = []
            for end in range(start, n):
                bucket = buckets[end]
                interval.append(bucket)
                
                interval_width = interval[-1].tmax_f - interval[0].tmin_f
                if interval_width > self.max_interval_width:
                    break
                
                # Calculate model probability
                from scipy import stats
                dist = stats.norm(loc=mu, scale=sigma)
                p_model = dist.cdf(interval[-1].tmax_f) - dist.cdf(interval[0].tmin_f)
                
                # Calculate implied cost
                implied = sum(b.market_ask for b in interval)
                
                edge = p_model - implied
                
                if edge > best_edge and edge >= self.edge_buffer:
                    best_edge = edge
                    best_interval = list(interval)
                    best_p = p_model
                    best_implied = implied
        
        if best_interval:
            return (best_interval, best_p, best_implied)
        return None
    
    def run_single_day(self, target_date: date, actual_high: float,
                        forecast_offset_days: int = 1) -> BacktestDay:
        """
        Simulate one day of trading.
        
        Args:
            target_date: The date being traded
            actual_high: The actual observed high temperature
            forecast_offset_days: Days ahead we're "forecasting" (affects sigma)
        """
        # Generate simulated forecast (actual high + forecast error)
        forecast_error = np.random.normal(0, 1.5 + forecast_offset_days * 0.5)
        forecast_mu = actual_high + forecast_error
        
        # Get sigma based on horizon
        sigma_config = self.config.get("sigma_by_horizon", {})
        forecast_sigma = sigma_config.get(str(forecast_offset_days), 
                                          sigma_config.get(forecast_offset_days, 2.5))
        
        forecast = DailyForecast(
            target_date=target_date,
            location=self.config.get("location", "London"),
            high_temp_f=forecast_mu,
            uncertainty_sigma_f=forecast_sigma
        )
        
        # Generate market buckets
        buckets = self.market_sim.generate_bucket_prices(
            actual_high,
            bucket_range=(actual_high - 15, actual_high + 15)
        )
        
        # Find best interval
        result = self._find_best_interval(buckets, forecast)
        
        if result is None:
            # No trade
            return BacktestDay(
                target_date=target_date,
                actual_high_f=actual_high,
                forecast_mu=forecast_mu,
                forecast_sigma=forecast_sigma,
                chosen_interval=None,
                interval_p_model=0,
                interval_implied=0,
                edge=0,
                cost=0,
                payout=0,
                hit=False,
                pnl=0
            )
        
        interval, p_model, implied = result
        interval_tmin = interval[0].tmin_f
        interval_tmax = interval[-1].tmax_f
        edge = p_model - implied
        
        # Calculate sizing
        shares = int(self.max_risk / implied) if implied > 0 else 0
        if shares < 1:
            shares = 1
        
        cost = shares * implied
        payout = shares  # Payout if hit
        
        # Check if we hit
        hit = interval_tmin <= actual_high < interval_tmax
        pnl = (payout - cost) if hit else -cost
        
        return BacktestDay(
            target_date=target_date,
            actual_high_f=actual_high,
            forecast_mu=forecast_mu,
            forecast_sigma=forecast_sigma,
            chosen_interval=(interval_tmin, interval_tmax),
            interval_p_model=p_model,
            interval_implied=implied,
            edge=edge,
            cost=cost,
            payout=payout,
            hit=hit,
            pnl=pnl
        )
    
    def run_backtest(self, start_date: date, end_date: date) -> List[BacktestDay]:
        """Run full backtest over a date range."""
        # Load historical data
        historical = self.weather.load_historical(start_date, end_date)
        
        results = []
        for target_date, actual_high in sorted(historical.items()):
            result = self.run_single_day(target_date, actual_high)
            results.append(result)
        
        return results


def print_backtest_summary(results: List[BacktestDay]):
    """Print summary statistics from backtest."""
    if not results:
        print("No results to summarize")
        return
    
    trades = [r for r in results if r.chosen_interval is not None]
    
    print("\n" + "="*70)
    print("  BACKTEST SUMMARY")
    print("="*70)
    
    print(f"\nTotal days analyzed: {len(results)}")
    print(f"Days with trades: {len(trades)}")
    print(f"Trade rate: {len(trades)/len(results)*100:.1f}%")
    
    if not trades:
        print("\nNo trades were made during the backtest period.")
        return
    
    # Win rate
    wins = [t for t in trades if t.hit]
    win_rate = len(wins) / len(trades)
    print(f"\nWin rate: {win_rate:.1%} ({len(wins)}/{len(trades)})")
    
    # P&L
    total_pnl = sum(t.pnl for t in trades)
    avg_pnl = total_pnl / len(trades)
    
    print(f"\nP&L Statistics:")
    print(f"  Total P&L: ${total_pnl:.2f}")
    print(f"  Average P&L per trade: ${avg_pnl:.2f}")
    
    winning_pnl = sum(t.pnl for t in wins) if wins else 0
    losing_pnl = sum(t.pnl for t in trades if not t.hit)
    
    print(f"  Total winning: ${winning_pnl:.2f}")
    print(f"  Total losing: ${losing_pnl:.2f}")
    
    if len(wins) > 0:
        avg_win = winning_pnl / len(wins)
        print(f"  Average win: ${avg_win:.2f}")
    
    losers = [t for t in trades if not t.hit]
    if len(losers) > 0:
        avg_loss = losing_pnl / len(losers)
        print(f"  Average loss: ${avg_loss:.2f}")
    
    # Edge analysis
    edges = [t.edge for t in trades]
    print(f"\nEdge Statistics:")
    print(f"  Average edge: {np.mean(edges):.2%}")
    print(f"  Min edge: {np.min(edges):.2%}")
    print(f"  Max edge: {np.max(edges):.2%}")
    
    # Forecast accuracy
    forecast_errors = [abs(t.forecast_mu - t.actual_high_f) for t in trades]
    print(f"\nForecast Error:")
    print(f"  Mean absolute error: {np.mean(forecast_errors):.1f}°F")
    print(f"  Max error: {np.max(forecast_errors):.1f}°F")
    
    # Model calibration
    predicted_wins = sum(t.interval_p_model for t in trades)
    print(f"\nModel Calibration:")
    print(f"  Predicted wins (sum of P): {predicted_wins:.1f}")
    print(f"  Actual wins: {len(wins)}")
    print(f"  Calibration ratio: {len(wins)/predicted_wins:.2f}" if predicted_wins > 0 else "  N/A")
    
    print("\n" + "="*70)


def print_trade_details(results: List[BacktestDay], limit: int = 20):
    """Print details of individual trades."""
    trades = [r for r in results if r.chosen_interval is not None]
    
    print(f"\nTrade Details (showing {min(limit, len(trades))} of {len(trades)}):\n")
    print(f"{'Date':<12} {'Actual':>7} {'Fcst':>7} {'Interval':>12} {'P_mod':>7} {'Impl':>7} {'Edge':>7} {'Hit':>5} {'PnL':>8}")
    print("-" * 80)
    
    for trade in trades[:limit]:
        interval_str = f"{trade.chosen_interval[0]:.0f}-{trade.chosen_interval[1]:.0f}" if trade.chosen_interval else "N/A"
        hit_str = "Y" if trade.hit else "N"
        pnl_str = f"${trade.pnl:+.2f}"
        
        print(f"{trade.target_date} {trade.actual_high_f:>7.1f} {trade.forecast_mu:>7.1f} "
              f"{interval_str:>12} {trade.interval_p_model:>7.2%} {trade.interval_implied:>7.2%} "
              f"{trade.edge:>7.2%} {hit_str:>5} {pnl_str:>8}")


def main():
    parser = argparse.ArgumentParser(description="Backtest London temperature trading strategy")
    parser.add_argument("--config", default="bot/config.yaml", help="Path to config file")
    parser.add_argument("--days", type=int, default=365, help="Number of days to backtest")
    parser.add_argument("--start-date", default=None, help="Start date (YYYY-MM-DD)")
    parser.add_argument("--verbose", "-v", action="store_true", help="Verbose output")
    parser.add_argument("--details", action="store_true", help="Show individual trade details")
    args = parser.parse_args()
    
    setup_logging(args.verbose)
    
    print("\n" + "="*70)
    print("  POLYMARKET TEMPERATURE STRATEGY BACKTEST")
    print("="*70)
    
    # Load config
    try:
        config = load_config(args.config)
    except FileNotFoundError:
        print(f"❌ Config file not found: {args.config}")
        sys.exit(1)
    
    # Determine date range
    if args.start_date:
        start_date = datetime.strptime(args.start_date, "%Y-%m-%d").date()
    else:
        start_date = date.today() - timedelta(days=args.days)
    
    end_date = start_date + timedelta(days=args.days)
    
    print(f"\n[Period] Backtest period: {start_date} to {end_date} ({args.days} days)")
    print(f"[Location] {config.get('location', 'London')}")
    print(f"[Edge] Edge buffer: {config.get('edge_buffer', 0.02):.2%}")
    print(f"[Risk] Max risk/day: ${config.get('max_risk_per_day_usd', 10)}")
    
    # Run backtest
    print("\n[Running] Backtest...")
    engine = BacktestEngine(config)
    results = engine.run_backtest(start_date, end_date)
    
    # Print results
    print_backtest_summary(results)
    
    if args.details:
        print_trade_details(results)
    
    print("\n[!] Note: This backtest uses simulated market prices since historical")
    print("    orderbook data is not available. Results are indicative only.")
    print("    Real trading results may differ significantly.\n")


if __name__ == "__main__":
    main()

