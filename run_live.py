#!/usr/bin/env python3
"""
run_live.py - Live trading (DISABLED BY DEFAULT)

Places real orders on Polymarket. Requires API keys and explicit configuration.

⚠️  WARNING: This places REAL orders with REAL money!

Prerequisites:
1. Set live_enabled: true in config.yaml
2. Set dry_run: false in config.yaml  
3. Set API credentials via environment variables:
   - POLYMARKET_API_KEY
   - POLYMARKET_API_SECRET

Usage:
    python run_live.py [--config CONFIG_PATH] [--confirm]
"""

import argparse
import logging
import os
import sys
from datetime import datetime
from pathlib import Path

import yaml

# Add bot directory to path
sys.path.insert(0, str(Path(__file__).parent))

from bot.gamma import GammaClient
from bot.clob import LiveCLOBClient, CLOBClient
from bot.model import TemperatureModel
from bot.strategy_interval import IntervalStrategy
from bot.risk import RiskManager
from bot.store import Store


def setup_logging(verbose: bool = False):
    """Configure logging with file output."""
    level = logging.DEBUG if verbose else logging.INFO
    
    # Create logs directory
    Path("logs").mkdir(exist_ok=True)
    log_file = f"logs/live_{datetime.now().strftime('%Y%m%d_%H%M%S')}.log"
    
    logging.basicConfig(
        level=level,
        format='%(asctime)s | %(levelname)-8s | %(name)s | %(message)s',
        datefmt='%Y-%m-%d %H:%M:%S',
        handlers=[
            logging.StreamHandler(),
            logging.FileHandler(log_file)
        ]
    )
    
    return log_file


def load_config(config_path: str = "bot/config.yaml") -> dict:
    """Load configuration from YAML file."""
    with open(config_path) as f:
        return yaml.safe_load(f)


def check_prerequisites(config: dict) -> tuple[bool, list[str]]:
    """
    Check all prerequisites for live trading.
    Returns (all_ok, list_of_issues).
    """
    issues = []
    
    # Check config flags
    if config.get("dry_run", True):
        issues.append("dry_run is True in config (must be False for live trading)")
    
    if not config.get("live_enabled", False):
        issues.append("live_enabled is False in config (must be True)")
    
    # Check API credentials
    api_key = os.environ.get("POLYMARKET_API_KEY") or config.get("api_key")
    api_secret = os.environ.get("POLYMARKET_API_SECRET") or config.get("api_secret")
    
    if not api_key:
        issues.append("POLYMARKET_API_KEY not set (env var or config)")
    
    if not api_secret:
        issues.append("POLYMARKET_API_SECRET not set (env var or config)")
    
    return len(issues) == 0, issues


def print_warning_banner():
    """Print scary warning banner."""
    print("\n" + "!"*70)
    print("\n  [WARNING] LIVE TRADING MODE [WARNING]")
    print("\n  This will place REAL orders with REAL money!")
    print("  Orders will be submitted to Polymarket.")
    print("  Losses are possible and irreversible.")
    print("\n" + "!"*70 + "\n")


def confirm_trading() -> bool:
    """Get explicit confirmation from user."""
    print("Type 'I UNDERSTAND THE RISKS' to proceed: ", end="")
    try:
        response = input()
        return response.strip() == "I UNDERSTAND THE RISKS"
    except EOFError:
        return False


def main():
    parser = argparse.ArgumentParser(description="Live trade London temperature intervals")
    parser.add_argument("--config", default="bot/config.yaml", help="Path to config file")
    parser.add_argument("--verbose", "-v", action="store_true", help="Verbose output")
    parser.add_argument("--confirm", action="store_true", help="Skip interactive confirmation")
    parser.add_argument("--check-only", action="store_true", help="Just check prerequisites")
    args = parser.parse_args()
    
    log_file = setup_logging(args.verbose)
    logger = logging.getLogger("live")
    
    print("\n" + "="*70)
    print("  POLYMARKET LIVE TRADING - LONDON TEMPERATURE")
    print("="*70)
    
    # Load config
    try:
        config = load_config(args.config)
    except FileNotFoundError:
        print(f"❌ Config file not found: {args.config}")
        sys.exit(1)
    
    # Check prerequisites
    print("\n🔍 Checking prerequisites...")
    all_ok, issues = check_prerequisites(config)
    
    if issues:
        print("\n[X] Prerequisites not met:\n")
        for issue in issues:
            print(f"   - {issue}")
        print("\n[*] To enable live trading:")
        print("   1. Edit bot/config.yaml:")
        print("      - Set 'dry_run: false'")
        print("      - Set 'live_enabled: true'")
        print("   2. Set environment variables:")
        print("      - POLYMARKET_API_KEY=your_key")
        print("      - POLYMARKET_API_SECRET=your_secret")
        print("\n   Then run this script again.\n")
        sys.exit(1)
    
    print("[OK] All prerequisites met")
    
    if args.check_only:
        print("\n--check-only specified, exiting without trading.\n")
        sys.exit(0)
    
    # Show warning and get confirmation
    print_warning_banner()
    
    print(f"[Config]")
    print(f"   Location: {config.get('location', 'London')}")
    print(f"   Max risk/day: ${config.get('max_risk_per_day_usd', 10)}")
    print(f"   Max total risk: ${config.get('max_total_open_risk_usd', 30)}")
    print(f"   Edge buffer: {config.get('edge_buffer', 0.02):.2%}")
    print(f"   Log file: {log_file}")
    
    if not args.confirm:
        if not confirm_trading():
            print("\n[X] Confirmation not received. Exiting.\n")
            sys.exit(1)
    else:
        logger.warning("--confirm flag used, skipping interactive confirmation")
    
    print("\n[OK] Confirmation received. Starting live trading...\n")
    logger.info("="*50)
    logger.info("LIVE TRADING SESSION STARTED")
    logger.info("="*50)
    
    # Initialize components
    store = Store(config.get("db_path", "bot_data.db"))
    gamma = GammaClient(config=config)
    strategy = IntervalStrategy(config=config)
    risk_mgr = RiskManager(config=config)
    
    # Get API credentials
    api_key = os.environ.get("POLYMARKET_API_KEY") or config.get("api_key")
    api_secret = os.environ.get("POLYMARKET_API_SECRET") or config.get("api_secret")
    
    try:
        # Initialize live CLOB client
        clob = LiveCLOBClient(api_key, api_secret, config=config)
        logger.info("Live CLOB client initialized")
    except NotImplementedError as e:
        print(f"\n[X] {e}")
        print("\nLive order placement requires the py-clob-client library.")
        print("This is a safety feature - the live client is intentionally not implemented.")
        print("\nTo actually place orders:")
        print("1. Install: pip install py-clob-client")
        print("2. Implement the LiveCLOBClient._authenticate() and place_order() methods")
        print("3. Test thoroughly on small amounts first!")
        sys.exit(1)
    except Exception as e:
        logger.error(f"Failed to initialize live client: {e}")
        print(f"\n[X] Failed to initialize live trading: {e}")
        sys.exit(1)
    
    # Discover markets
    location = config.get("location", "London")
    logger.info(f"Discovering markets for {location}...")
    
    try:
        markets = gamma.discover_bucket_markets(location=location)
        logger.info(f"Found {len(markets)} bucket markets")
    except Exception as e:
        logger.error(f"Market discovery failed: {e}")
        print(f"[X] Failed to discover markets: {e}")
        sys.exit(1)
    
    if not markets:
        print("[X] No temperature markets found")
        sys.exit(0)
    
    # Find opportunities
    plans = strategy.scan_all_dates(markets, location=location)
    
    if not plans:
        print("[i] No trading opportunities found")
        logger.info("No opportunities found, exiting")
        sys.exit(0)
    
    print(f"\n[+] Found {len(plans)} opportunities")
    
    for plan in plans:
        print(f"\n{'='*60}")
        print(f"TRADE: {plan.target_date} | {plan.interval_str}")
        print(f"{'='*60}")
        print(f"Edge: {plan.edge:.2%} | Cost: ${plan.total_cost:.2f}")
        
        # Check risk
        can_trade, reason = risk_mgr.check_trade(
            plan.total_cost,
            [leg.token_id for leg in plan.legs]
        )
        
        if not can_trade:
            print(f"[X] BLOCKED: {reason}")
            logger.warning(f"Trade blocked: {reason}")
            continue
        
        # Would place orders here
        print("[~] Would place orders for:")
        for leg in plan.legs:
            print(f"   BUY {leg.shares:.0f} YES {leg.market.tmin_f}-{leg.market.tmax_f} F @ {leg.limit_price:.4f}")
        
        logger.info(f"Trade plan ready: {plan.interval_str}, cost=${plan.total_cost:.2f}")
        
        # NOTE: Actual order placement would happen here
        # This is intentionally left as a placeholder for safety
        print("\n[!] Order placement not implemented (safety feature)")
    
    print("\n" + "="*70)
    print("Live trading session complete")
    print("="*70)
    
    risk_mgr.print_status()
    
    logger.info("="*50)
    logger.info("LIVE TRADING SESSION ENDED")
    logger.info("="*50)


if __name__ == "__main__":
    main()

