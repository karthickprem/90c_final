#!/usr/bin/env python3
"""
run_scan.py - Scan for trading opportunities

Discovers temperature bucket markets and identifies intervals
where the model probability exceeds market implied price by the edge buffer.

Usage:
    python run_scan.py [--config CONFIG_PATH] [--verbose] [--debug]
    python run_scan.py --cities london,nyc,chicago
"""

import argparse
import logging
import sys
from datetime import date
from pathlib import Path

import yaml

# Add bot directory to path
sys.path.insert(0, str(Path(__file__).parent))

from bot.gamma import GammaClient, group_markets_by_date, group_markets_by_location_date
from bot.clob import CLOBClient
from bot.model import TemperatureModel
from bot.strategy_interval import IntervalStrategy, print_trade_plan
from bot.store import Store


def setup_logging(verbose: bool = False):
    """Configure logging."""
    level = logging.DEBUG if verbose else logging.INFO
    logging.basicConfig(
        level=level,
        format='%(asctime)s | %(levelname)-8s | %(name)s | %(message)s',
        datefmt='%Y-%m-%d %H:%M:%S'
    )


def load_config(config_path: str = "bot/config.yaml") -> dict:
    """Load configuration from YAML file."""
    with open(config_path) as f:
        return yaml.safe_load(f)


def print_header():
    """Print scan header."""
    print("\n" + "="*70)
    print("  POLYMARKET TEMPERATURE SCANNER")
    print("="*70)


def print_market_summary(markets_by_loc_date: dict):
    """Print summary of discovered markets."""
    total_markets = sum(len(m) for m in markets_by_loc_date.values())
    total_groups = len(markets_by_loc_date)
    
    print(f"\n[Markets] {total_markets} buckets across {total_groups} location-dates")
    
    for (loc, d), markets in sorted(markets_by_loc_date.items()):
        buckets = [(m.tmin_f, m.tmax_f) for m in markets]
        min_t = min(b[0] for b in buckets)
        max_t = max(b[1] for b in buckets)
        closed = "[CLOSED]" if markets[0].closed else "[ACTIVE]"
        print(f"  {closed} {loc.title()} {d}: {len(markets)} buckets ({min_t:.0f}-{max_t:.0f}F)")


def print_forecast_summary(forecasts: dict):
    """Print forecast summary."""
    print("\n[Forecasts]")
    for (loc, d), fc in sorted(forecasts.items()):
        sigma_info = f"sigma={fc.uncertainty_sigma_f:.1f}F"
        print(f"  {loc.title()} {d}: mu={fc.high_temp_f:.1f}F, {sigma_info}")


def print_opportunities(plans: list):
    """Print trading opportunities."""
    if not plans:
        print("\n[X] No trading opportunities found")
        return
    
    print(f"\n[OK] Found {len(plans)} trading opportunities:\n")
    
    for plan in sorted(plans, key=lambda p: p.edge, reverse=True):
        print(f"[+] {plan.target_date} {plan.location} | {plan.interval_str}")
        print(f"    Forecast: mu={plan.forecast_mu:.1f}F, sigma={plan.forecast_sigma:.1f}F (k={plan.sigma_k:.2f})")
        print(f"    P_model: {plan.p_interval:.4f} | Implied: {plan.implied_cost:.4f} | Edge: {plan.edge:.4f}")
        print(f"    Trade: {plan.shares_per_leg:.0f} shares x {plan.num_buckets} buckets = ${plan.total_cost:.2f}")
        print(f"    Expected PnL: ${plan.expected_pnl:.2f}")
        print()
        
        # Show per-bucket prices
        print("    Per-bucket executable prices:")
        for tmin, tmax, price in plan.per_bucket_prices:
            width = tmax - tmin
            if abs(width - round(width)) > 0.1:  # Non-integer width (Celsius)
                print(f"      {tmin:.1f}-{tmax:.1f}F: {price:.4f}")
            else:
                print(f"      {tmin:.0f}-{tmax:.0f}F: {price:.4f}")
        print()


def main():
    parser = argparse.ArgumentParser(description="Scan for temperature trading opportunities")
    parser.add_argument("--config", default="bot/config.yaml", help="Path to config file")
    parser.add_argument("--verbose", "-v", action="store_true", help="Verbose output")
    parser.add_argument("--debug", action="store_true", help="Show market discovery debug stats")
    parser.add_argument("--cities", default=None, help="Comma-separated cities to scan")
    parser.add_argument("--include-closed", action="store_true", help="Also scan closed markets (for testing)")
    parser.add_argument("--all-cities", action="store_true", help="Scan all available cities")
    args = parser.parse_args()
    
    setup_logging(args.verbose)
    logger = logging.getLogger("scanner")
    
    print_header()
    
    # Load config
    try:
        config = load_config(args.config)
        logger.info(f"Loaded config from {args.config}")
    except FileNotFoundError:
        print(f"[X] Config file not found: {args.config}")
        sys.exit(1)
    
    # Determine cities to scan
    if args.cities:
        locations = [c.strip() for c in args.cities.split(",")]
    else:
        locations = config.get("locations", [config.get("primary_location", "London")])
    
    min_edge = config.get("min_edge", 0.03)
    edge_buffer = config.get("edge_buffer", 0.02)
    total_edge = min_edge + edge_buffer
    
    print(f"\n[Config]")
    print(f"   Locations: {', '.join(locations)}")
    print(f"   Min edge: {min_edge:.2%} + buffer {edge_buffer:.2%} = {total_edge:.2%} total")
    print(f"   Max risk/day: ${config.get('max_risk_per_day_usd', 10)}")
    print(f"   Max interval width: {config.get('interval_max_width_f', 6)}F")
    
    # Initialize components
    store = Store(config.get("db_path", "bot_data.db"))
    gamma = GammaClient(config=config)
    
    # Discover markets
    print(f"\n[Search] Discovering temperature markets...")
    try:
        # Use events_status for public-search API
        events_status = "all" if args.include_closed else "active"
        markets = gamma.discover_bucket_markets(
            locations=None if args.all_cities else locations,
            events_status=events_status,
            debug=args.debug
        )
    except Exception as e:
        print(f"[X] Failed to discover markets: {e}")
        logger.exception("Market discovery failed")
        sys.exit(1)
    
    # Print debug stats if requested
    if args.debug:
        gamma.print_debug_stats()
    
    if not markets:
        print(f"[X] No temperature bucket markets found")
        print("   This could mean:")
        print("   - No active temperature markets exist on Polymarket")
        print("   - Markets use different question format")
        print("   - Try --include-closed to test parser on closed markets")
        print("   - Try --debug to see what markets were scanned")
        sys.exit(0)
    
    # Group by location and date
    markets_by_loc_date = group_markets_by_location_date(markets)
    print_market_summary(markets_by_loc_date)
    
    # Get forecasts and scan for opportunities
    model = TemperatureModel(config=config)
    forecasts = {}
    
    # Get forecasts for active markets only
    active_markets = [m for m in markets if not m.closed]
    if active_markets:
        for (loc, d) in set((m.location.lower(), m.target_date) for m in active_markets):
            fc = model.get_forecast(loc, d)
            if fc:
                forecasts[(loc, d)] = fc
                store.log_forecast(d, loc, fc.high_temp_f, fc.uncertainty_sigma_f, fc.source)
        
        print_forecast_summary(forecasts)
    
    # Find opportunities
    print("\n[Analyze] Scanning for opportunities...")
    
    strategy = IntervalStrategy(config=config)
    all_plans = []
    
    for loc in locations:
        loc_markets = [m for m in active_markets if m.location.lower() == loc.lower()]
        if loc_markets:
            plans = strategy.scan_all_dates(loc_markets, location=loc)
            all_plans.extend(plans)
    
    # Log signals to store
    for plan in all_plans:
        interval_id = f"{plan.target_date}_{plan.location}_{plan.interval_tmin}_{plan.interval_tmax}"
        store.log_signal(
            target_date=plan.target_date,
            interval_id=interval_id,
            interval_tmin=plan.interval_tmin,
            interval_tmax=plan.interval_tmax,
            implied_cost=plan.implied_cost,
            p_model=plan.p_interval,
            edge=plan.edge,
            forecast_mu=plan.forecast_mu,
            forecast_sigma=plan.forecast_sigma,
            chosen=True,
            reason="best_edge"
        )
    
    print_opportunities(all_plans)
    
    # Print skip summary
    strategy.print_skip_summary()
    
    # Summary
    print("="*70)
    print(f"Scan complete. {len(all_plans)} opportunities, {len(strategy.skipped)} skipped.")
    print("="*70)
    
    if all_plans:
        print("\nNext steps:")
        print("  1. Review opportunities above")
        print("  2. Run 'python run_paper.py --once' to simulate trades")
        print("  3. When ready, configure API keys and run 'python run_live.py'")


if __name__ == "__main__":
    main()
