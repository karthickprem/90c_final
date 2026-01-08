#!/usr/bin/env python3
"""
run_paper.py - Paper trading simulation

Simulates trading the best interval opportunities without placing real orders.
Uses current market prices to simulate fills and logs for future settlement.

Usage:
    python run_paper.py [--config CONFIG_PATH] [--verbose] [--once]
    python run_paper.py --log-only  # Just log opportunities, don't simulate fills
"""

import argparse
import logging
import sys
import time
import json
from datetime import date, datetime
from pathlib import Path
from uuid import uuid4

import yaml

# Add bot directory to path
sys.path.insert(0, str(Path(__file__).parent))

from bot.gamma import GammaClient, group_markets_by_location_date
from bot.clob import PaperCLOBClient, Side, Order, OrderStatus
from bot.model import TemperatureModel
from bot.strategy_interval import IntervalStrategy, TradePlan, print_trade_plan
from bot.risk import RiskManager, Position
from bot.store import Store
from bot.resolution import interval_hit


def setup_logging(verbose: bool = False, log_file: str = None):
    """Configure logging."""
    level = logging.DEBUG if verbose else logging.INFO
    
    handlers = [logging.StreamHandler()]
    if log_file:
        handlers.append(logging.FileHandler(log_file))
    
    logging.basicConfig(
        level=level,
        format='%(asctime)s | %(levelname)-8s | %(name)s | %(message)s',
        datefmt='%Y-%m-%d %H:%M:%S',
        handlers=handlers
    )


def load_config(config_path: str = "bot/config.yaml") -> dict:
    """Load configuration from YAML file."""
    with open(config_path) as f:
        return yaml.safe_load(f)


def log_paper_entry(store: Store, plan: TradePlan, position_id: str):
    """Log paper trade entry with full detail for later settlement."""
    # Log position
    store.log_position(
        position_id=position_id,
        target_date=plan.target_date,
        interval_tmin=plan.interval_tmin,
        interval_tmax=plan.interval_tmax,
        total_cost=plan.total_cost,
        payout_if_hit=plan.payout_if_hit,
        shares_per_leg=plan.shares_per_leg,
        token_ids=[leg.token_id for leg in plan.legs]
    )
    
    # Log each order
    for leg in plan.legs:
        store.log_order(
            order_id=f"paper_{uuid4().hex[:8]}",
            position_id=position_id,
            token_id=leg.token_id,
            side="BUY",
            shares=leg.shares,
            limit_price=leg.limit_price,
            status="FILLED",
            filled_shares=leg.shares,
            avg_fill_price=leg.limit_price
        )
    
    # Log signal
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
        reason=f"paper_entry|pos={position_id}"
    )


def simulate_trade(plan: TradePlan, clob: PaperCLOBClient, 
                   risk_mgr: RiskManager, store: Store,
                   log_only: bool = False) -> bool:
    """
    Simulate executing a trade plan.
    Returns True if successful.
    """
    logger = logging.getLogger("paper")
    
    # Check risk limits
    can_trade, reason = risk_mgr.check_trade(
        cost=plan.total_cost,
        token_ids=[leg.token_id for leg in plan.legs]
    )
    
    if not can_trade:
        logger.warning(f"Trade blocked: {reason}")
        return False
    
    # Generate position ID
    position_id = f"paper_{uuid4().hex[:8]}"
    
    if log_only:
        # Just log, don't simulate fills
        log_paper_entry(store, plan, position_id)
        logger.info(f"Logged opportunity: {plan.interval_str} (log-only mode)")
        return True
    
    # Simulate order execution for each leg
    orders = plan.to_orders(slippage_cap=0.005)
    all_filled = True
    total_filled_cost = 0
    
    for order in orders:
        filled_order = clob.place_order(order)
        
        if filled_order.status != OrderStatus.FILLED:
            logger.warning(f"Order failed to fill: {order.token_id}")
            all_filled = False
            break
        
        total_filled_cost += filled_order.filled_shares * filled_order.avg_fill_price
    
    if not all_filled:
        logger.warning("Trade partially filled - would cancel remaining in live mode")
        return False
    
    # Create position
    position = Position(
        position_id=position_id,
        target_date=plan.target_date,
        interval_tmin=plan.interval_tmin,
        interval_tmax=plan.interval_tmax,
        total_cost=total_filled_cost,
        payout_if_hit=plan.payout_if_hit,
        entry_time=datetime.now(),
        token_ids=[leg.token_id for leg in plan.legs],
        shares_per_leg=plan.shares_per_leg
    )
    
    # Register with risk manager
    risk_mgr.register_trade(position)
    
    # Log to database
    log_paper_entry(store, plan, position_id)
    
    return True


def print_simulation_result(plan: TradePlan, success: bool):
    """Print simulation result with pricing transparency."""
    status = "[OK] SIMULATED" if success else "[X] BLOCKED"
    
    print(f"\n{status}: {plan.target_date} {plan.location} | {plan.interval_str}")
    print(f"  Forecast: mu={plan.forecast_mu:.1f}F, sigma={plan.forecast_sigma:.1f}F")
    print(f"  P_model: {plan.p_interval:.4f} | Implied: {plan.implied_cost:.4f} | Edge: {plan.edge:.4f}")
    print(f"  Cost: ${plan.total_cost:.2f} | Payout if hit: ${plan.payout_if_hit:.2f}")
    
    if success:
        # Show P&L scenarios
        profit_if_hit = plan.payout_if_hit - plan.total_cost
        print(f"\n  Scenarios:")
        print(f"    If temp IN  [{plan.interval_tmin:.0f}, {plan.interval_tmax:.0f})F: +${profit_if_hit:.2f}")
        print(f"    If temp OUT [{plan.interval_tmin:.0f}, {plan.interval_tmax:.0f})F: -${plan.total_cost:.2f}")
        
        # Show per-bucket prices
        print(f"\n  Per-bucket prices (depth-walked):")
        for tmin, tmax, price in plan.per_bucket_prices:
            print(f"    {tmin:.0f}-{tmax:.0f}F: {price:.4f}")


def run_scan_cycle(config: dict, gamma: GammaClient, strategy: IntervalStrategy,
                   clob: PaperCLOBClient, risk_mgr: RiskManager, store: Store,
                   log_only: bool = False) -> list:
    """Run one scan and trade cycle."""
    logger = logging.getLogger("paper")
    
    locations = config.get("locations", [config.get("primary_location", "London")])
    
    # Discover markets
    logger.info("Scanning for markets...")
    markets = gamma.discover_bucket_markets(locations=locations)
    
    if not markets:
        logger.warning("No temperature markets found")
        return []
    
    # Filter to active only
    active_markets = [m for m in markets if not m.closed]
    logger.info(f"Found {len(active_markets)} active bucket markets")
    
    if not active_markets:
        return []
    
    # Find opportunities per location
    all_plans = []
    for loc in locations:
        loc_markets = [m for m in active_markets if m.location.lower() == loc.lower()]
        if loc_markets:
            plans = strategy.scan_all_dates(loc_markets, location=loc)
            all_plans.extend(plans)
    
    if not all_plans:
        logger.info("No trading opportunities found")
        return []
    
    logger.info(f"Found {len(all_plans)} opportunities")
    
    # Simulate trades
    results = []
    for plan in all_plans:
        success = simulate_trade(plan, clob, risk_mgr, store, log_only=log_only)
        results.append((plan, success))
        print_simulation_result(plan, success)
    
    return results


def main():
    parser = argparse.ArgumentParser(description="Paper trade temperature intervals")
    parser.add_argument("--config", default="bot/config.yaml", help="Path to config file")
    parser.add_argument("--verbose", "-v", action="store_true", help="Verbose output")
    parser.add_argument("--once", action="store_true", help="Run once and exit (don't loop)")
    parser.add_argument("--interval", type=float, default=3600, help="Scan interval in seconds (default: 1 hour)")
    parser.add_argument("--log-only", action="store_true", help="Just log opportunities, don't simulate fills")
    args = parser.parse_args()
    
    setup_logging(args.verbose, log_file="paper_trading.log")
    logger = logging.getLogger("paper")
    
    # Load config
    try:
        config = load_config(args.config)
    except FileNotFoundError:
        print(f"[X] Config file not found: {args.config}")
        sys.exit(1)
    
    locations = config.get("locations", [config.get("primary_location", "London")])
    min_edge = config.get("min_edge", 0.03)
    edge_buffer = config.get("edge_buffer", 0.02)
    
    print("\n" + "="*70)
    print("  POLYMARKET PAPER TRADING - TEMPERATURE")
    print("="*70)
    print(f"\n[Mode] {'Single scan' if args.once else f'Continuous (every {args.interval}s)'}")
    print(f"   Locations: {', '.join(locations)}")
    print(f"   Min edge: {min_edge + edge_buffer:.2%}")
    print(f"   Max risk/day: ${config.get('max_risk_per_day_usd', 10)}")
    if args.log_only:
        print(f"   LOG-ONLY MODE: Not simulating fills")
    
    # Initialize components
    store = Store(config.get("db_path", "bot_data.db"))
    gamma = GammaClient(config=config)
    strategy = IntervalStrategy(config=config)
    clob = PaperCLOBClient(config=config)
    risk_mgr = RiskManager(config=config)
    
    try:
        if args.once:
            # Single scan
            results = run_scan_cycle(config, gamma, strategy, clob, risk_mgr, store, 
                                    log_only=args.log_only)
            
            print("\n" + "="*70)
            print("PAPER TRADING SUMMARY")
            print("="*70)
            
            successful = [r for r in results if r[1]]
            print(f"\nSimulated {len(successful)} of {len(results)} opportunities")
            
            # Show skip summary
            strategy.print_skip_summary()
            
            risk_mgr.print_status()
            
        else:
            # Continuous loop
            print("\n[Loop] Starting continuous paper trading loop...")
            print("   Press Ctrl+C to stop\n")
            
            while True:
                try:
                    run_scan_cycle(config, gamma, strategy, clob, risk_mgr, store,
                                  log_only=args.log_only)
                    risk_mgr.print_status()
                    
                    logger.info(f"Sleeping {args.interval}s until next scan...")
                    time.sleep(args.interval)
                    
                except KeyboardInterrupt:
                    raise
                except Exception as e:
                    logger.error(f"Scan cycle error: {e}")
                    risk_mgr.record_api_error()
                    time.sleep(60)  # Short sleep on error
    
    except KeyboardInterrupt:
        print("\n\n[Stop] Stopping paper trading...")
        risk_mgr.print_status()
        print("\nPaper trading session ended.")


if __name__ == "__main__":
    main()
