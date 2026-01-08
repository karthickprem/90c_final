#!/usr/bin/env python3
"""
Full-Set Arbitrage Bot - Main Runner

Paper trading bot for Polymarket full-set arbitrage strategy.

Strategy: Buy YES + NO when askYES + askNO < 1.00
- Guaranteed profit if both legs fill (hold to settlement = $1)
- Risk: one leg fills, other doesn't (must unwind at loss)

Usage:
    python run_fullset_arb.py              # Run continuous scanning
    python run_fullset_arb.py --once       # Single scan cycle
    python run_fullset_arb.py --stats      # Show statistics
    python run_fullset_arb.py --discover   # Just discover markets

This is PAPER TRADING only - no real orders are placed.
"""

import argparse
import logging
import signal
import sys
import time
from datetime import datetime

from fullset_arb.config import ArbConfig, load_config
from fullset_arb.market_discovery import MarketDiscovery
from fullset_arb.scanner import ArbScanner
from fullset_arb.executor import PaperExecutor, ExecutionStatus
from fullset_arb.ledger import Ledger
from fullset_arb.metrics import MetricsLogger, ConsoleReporter, PerformanceAnalyzer


# Global flag for graceful shutdown
running = True


def signal_handler(signum, frame):
    """Handle Ctrl+C gracefully."""
    global running
    print("\n\nShutting down gracefully...")
    running = False


def setup_logging(config: ArbConfig):
    """Configure logging."""
    log_format = "%(asctime)s | %(levelname)-8s | %(name)s | %(message)s"
    
    handlers = [
        logging.StreamHandler(sys.stdout),
    ]
    
    if config.log_file:
        handlers.append(logging.FileHandler(config.log_file))
    
    logging.basicConfig(
        level=getattr(logging, config.log_level.upper()),
        format=log_format,
        handlers=handlers,
    )
    
    # Reduce noise from requests library
    logging.getLogger("urllib3").setLevel(logging.WARNING)
    logging.getLogger("requests").setLevel(logging.WARNING)


def run_scan_cycle(scanner: ArbScanner, executor: PaperExecutor, 
                   ledger: Ledger, metrics: MetricsLogger,
                   reporter: ConsoleReporter, execute: bool = True) -> int:
    """
    Run a single scan cycle with VWAP-based edge calculation.
    
    1. Refresh market cache if needed
    2. Scan all markets for opportunities (using edge_exec)
    3. Execute actionable opportunities (paper) with instant redemption
    4. Log everything including edge distribution
    
    Returns number of executions.
    """
    start_time = time.time()
    
    # Get markets (uses cache if fresh)
    markets = scanner.discovery.get_cached_markets()
    
    # Scan for opportunities (uses VWAP-based edge_exec)
    opportunities = scanner.scan_all(markets)
    
    duration_ms = (time.time() - start_time) * 1000
    
    # Log all opportunities to JSONL (use edge_exec for filtering)
    for opp in opportunities:
        if opp.edge_exec > -0.02 or opp.is_actionable:  # Log interesting ones
            metrics.log_opportunity(opp)
            ledger.log_opportunity(opp)
    
    # Log scan cycle
    actionable = [o for o in opportunities if o.is_actionable]
    positive_edge_exec = [o for o in opportunities if o.edge_exec > 0]
    
    metrics.log_scan_cycle(
        markets_scanned=len(markets),
        opportunities_found=len(positive_edge_exec),
        actionable_found=len(actionable),
        duration_ms=duration_ms,
    )
    
    # Print summary
    reporter.print_scan_summary(len(markets), opportunities, duration_ms)
    
    # Print best edge each cycle (per ChatGPT recommendation)
    if opportunities:
        best = opportunities[0]  # Already sorted by edge_exec
        print(f"   Best edge_exec: {best.edge_exec:.4f} ({best.market.slug[:40]})")
        print(f"   Sum VWAP: {best.sum_vwap_asks:.4f} (L1 sum: {best.sum_asks:.4f})")
    
    # Execute actionable opportunities
    executions = 0
    
    if execute and actionable:
        for opp in actionable:
            # No position tracking needed - we redeem immediately!
            # But still respect max concurrent executions
            if executions >= executor.config.max_open_positions:
                logging.info("Max concurrent executions reached")
                break
            
            # Execute (paper) with instant redemption
            result = executor.execute(opp)
            
            # Log execution
            opp_id = ledger.log_opportunity(opp)
            exec_id = ledger.log_execution(result, opp_id)
            metrics.log_execution(result)
            
            # No position creation for SUCCESS - we redeemed immediately!
            # Positions only matter for tracking partial fills or unwinds
            
            # Print result
            reporter.print_execution_summary(result)
            
            executions += 1
            
            # Small delay between executions
            time.sleep(0.5)
    
    # Update daily summary
    ledger.update_daily_summary()
    
    return executions


def run_continuous(config: ArbConfig):
    """Run continuous scanning loop."""
    global running
    
    print("\n" + "="*60)
    print("FULL-SET ARBITRAGE BOT - PAPER TRADING")
    print("="*60)
    print(f"Started at: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    print(f"Database: {config.db_path}")
    print(f"Min edge: {config.min_edge:.4f} ({config.min_edge*100:.2f}%)")
    print(f"Scan interval: {config.scan_interval_seconds}s")
    print("="*60)
    print("\nPress Ctrl+C to stop.\n")
    
    # Initialize components
    discovery = MarketDiscovery(config)
    scanner = ArbScanner(config, discovery)
    executor = PaperExecutor(config, scanner)
    ledger = Ledger(config)
    metrics = MetricsLogger(config)
    reporter = ConsoleReporter(ledger)
    
    # Initial market discovery
    print("Discovering markets...")
    markets = discovery.discover_all()
    print(f"Found {len(markets)} suitable binary markets\n")
    
    cycle_count = 0
    total_executions = 0
    
    while running:
        try:
            cycle_count += 1
            
            executions = run_scan_cycle(
                scanner, executor, ledger, metrics, reporter,
                execute=True
            )
            total_executions += executions
            
            if running:
                # Print periodic summary
                if cycle_count % 10 == 0:
                    print(f"\n--- Cycle {cycle_count} | Total executions: {total_executions} ---")
                    stats = executor.get_stats()
                    print(f"    Success rate: {stats['success_rate_pct']:.1f}%")
                    print(f"    Total P&L: ${stats['total_pnl']:.4f}")
                
                time.sleep(config.scan_interval_seconds)
        
        except KeyboardInterrupt:
            break
        
        except Exception as e:
            logging.error(f"Error in scan cycle: {e}")
            metrics.log_error(str(e), {"cycle": cycle_count})
            time.sleep(5)  # Wait before retrying
    
    # Final summary
    print("\n" + "="*60)
    print("FINAL SUMMARY")
    print("="*60)
    reporter.print_overall_stats()
    
    print(f"\nTotal cycles: {cycle_count}")
    print(f"Total executions: {total_executions}")
    print(f"Uptime: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")


def run_once(config: ArbConfig):
    """Run a single scan cycle."""
    print("\nRunning single scan cycle...\n")
    
    discovery = MarketDiscovery(config)
    scanner = ArbScanner(config, discovery)
    executor = PaperExecutor(config, scanner)
    ledger = Ledger(config)
    metrics = MetricsLogger(config)
    reporter = ConsoleReporter(ledger)
    
    # Discover markets
    print("Discovering markets...")
    markets = discovery.discover_all()
    print(f"Found {len(markets)} suitable binary markets\n")
    
    # Run scan
    executions = run_scan_cycle(
        scanner, executor, ledger, metrics, reporter,
        execute=True
    )
    
    print(f"\nCompleted. Executions: {executions}")


def show_stats(config: ArbConfig):
    """Show statistics and analysis."""
    ledger = Ledger(config)
    reporter = ConsoleReporter(ledger)
    analyzer = PerformanceAnalyzer(ledger)
    
    reporter.print_overall_stats()
    reporter.print_daily_summary()
    reporter.print_pnl_curve(days=14)
    
    # Analysis
    print("\n" + "="*60)
    print("PERFORMANCE ANALYSIS")
    print("="*60)
    
    freq = analyzer.analyze_opportunity_frequency()
    if freq:
        print(f"\nOpportunity Frequency ({freq['days_analyzed']} days):")
        print(f"  Avg opportunities/day: {freq['avg_opportunities_per_day']:.1f}")
        print(f"  Avg actionable/day:    {freq['avg_actionable_per_day']:.1f}")
        print(f"  Actionable rate:       {freq['actionable_rate']:.1f}%")
    
    fill = analyzer.analyze_fill_quality()
    if fill:
        print(f"\nFill Quality:")
        print(f"  Fill rate:    {fill['fill_rate']:.1f}%")
        print(f"  One-leg rate: {fill['one_leg_rate']:.1f}%")
    
    sharpe = analyzer.calculate_sharpe_ratio()
    if sharpe:
        print(f"\nSharpe Ratio (annualized): {sharpe:.2f}")
    
    print("\n" + "="*60)
    print("RECOMMENDATIONS")
    print("="*60)
    for rec in analyzer.get_recommendations():
        print(f"  • {rec}")


def discover_only(config: ArbConfig):
    """Just discover and list markets."""
    discovery = MarketDiscovery(config)
    
    print("\nDiscovering markets...\n")
    markets = discovery.discover_all(max_markets=100)
    
    print(f"{'='*80}")
    print(f"Found {len(markets)} suitable binary markets")
    print(f"{'='*80}\n")
    
    for i, market in enumerate(markets[:30]):
        print(f"{i+1:3}. {market.question[:70]}")
        print(f"     Slug: {market.slug}")
        print(f"     Volume 24h: ${market.volume_24h:,.0f} | Liquidity: ${market.liquidity:,.0f}")
        print(f"     YES token: {market.yes_token_id[:30]}...")
        print(f"     NO token:  {market.no_token_id[:30]}...")
        print()


def main():
    """Main entry point."""
    parser = argparse.ArgumentParser(
        description="Full-Set Arbitrage Bot for Polymarket (Paper Trading)",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
    python run_fullset_arb.py              # Continuous scanning
    python run_fullset_arb.py --once       # Single scan
    python run_fullset_arb.py --stats      # View statistics
    python run_fullset_arb.py --discover   # List markets

This is PAPER TRADING - no real orders are placed.
        """
    )
    
    parser.add_argument("--once", action="store_true",
                       help="Run a single scan cycle and exit")
    parser.add_argument("--stats", action="store_true",
                       help="Show statistics and exit")
    parser.add_argument("--discover", action="store_true",
                       help="Discover markets and exit")
    parser.add_argument("--config", type=str, default=None,
                       help="Path to config YAML file")
    parser.add_argument("--min-edge", type=float, default=None,
                       help="Override minimum edge threshold")
    parser.add_argument("--interval", type=float, default=None,
                       help="Override scan interval (seconds)")
    parser.add_argument("--debug", action="store_true",
                       help="Enable debug logging")
    
    args = parser.parse_args()
    
    # Load config
    config = load_config(args.config)
    
    # Apply overrides
    if args.min_edge is not None:
        config.min_edge = args.min_edge
    if args.interval is not None:
        config.scan_interval_seconds = args.interval
    if args.debug:
        config.log_level = "DEBUG"
    
    # Setup logging
    setup_logging(config)
    
    # Setup signal handler
    signal.signal(signal.SIGINT, signal_handler)
    signal.signal(signal.SIGTERM, signal_handler)
    
    # Run appropriate mode
    if args.stats:
        show_stats(config)
    elif args.discover:
        discover_only(config)
    elif args.once:
        run_once(config)
    else:
        run_continuous(config)


if __name__ == "__main__":
    main()

