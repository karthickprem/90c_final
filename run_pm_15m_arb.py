#!/usr/bin/env python3
"""
BTC 15-Minute Up/Down Arbitrage Bot - Main Runner

Implements Variant A (paired full-set arb) for Polymarket BTC 15-minute markets.

Modes:
    --mode record   : Capture orderbooks to JSONL for later replay
    --mode paper    : Paper trade live markets
    --mode replay   : Simulate from recorded data

Usage:
    python run_pm_15m_arb.py --mode paper              # Paper trade live
    python run_pm_15m_arb.py --mode record             # Record orderbooks
    python run_pm_15m_arb.py --mode replay FILE        # Replay from recording
    python run_pm_15m_arb.py --mode paper --once       # Single window
    python run_pm_15m_arb.py --stats                   # Show statistics
    python run_pm_15m_arb.py --report                  # Generate report

This is PAPER TRADING only - no real orders are placed.
"""

import argparse
import logging
import signal
import sys
import time
from datetime import datetime
from pathlib import Path

# Add parent directory to path for imports
sys.path.insert(0, str(Path(__file__).parent))

from pm_15m_arb.config import ArbConfig, load_config
from pm_15m_arb.metrics import MetricsLogger, RecordingMetrics

# These will be imported as we implement them
# from pm_15m_arb.market_discovery import BTC15mMarketDiscovery
# from pm_15m_arb.orderbook import OrderbookFetcher
# from pm_15m_arb.recorder import Recorder, Replayer
# from pm_15m_arb.strategy import StrategyEngine
# from pm_15m_arb.executor_paper import PaperExecutor
# from pm_15m_arb.ledger import Ledger
# from pm_15m_arb.report import ReportGenerator


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


def check_kill_switch(config: ArbConfig) -> bool:
    """Check if kill switch file exists."""
    if Path(config.kill_switch_file).exists():
        logging.critical(f"KILL SWITCH detected: {config.kill_switch_file}")
        return True
    return False


def run_paper_mode(config: ArbConfig, once: bool = False):
    """Run paper trading on live markets."""
    global running
    
    print("\n" + "="*60)
    print("PM 15m ARB BOT - PAPER TRADING MODE")
    print("="*60)
    print(f"Started at: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    config.print_summary()
    print("="*60)
    print("\nPress Ctrl+C to stop.\n")
    
    # Import components (deferred to avoid circular imports)
    from pm_15m_arb.market_discovery import BTC15mMarketDiscovery
    from pm_15m_arb.orderbook import OrderbookFetcher
    from pm_15m_arb.strategy import StrategyEngine
    from pm_15m_arb.executor_paper import PaperExecutor
    from pm_15m_arb.ledger import Ledger
    
    # Initialize components
    metrics = MetricsLogger(config)
    discovery = BTC15mMarketDiscovery(config)
    orderbook = OrderbookFetcher(config)
    ledger = Ledger(config)
    executor = PaperExecutor(config, orderbook, metrics, ledger)
    strategy = StrategyEngine(config, executor, metrics, ledger)
    
    window_count = 0
    
    while running:
        try:
            # Check kill switch
            if check_kill_switch(config):
                break
            
            # Discover current/next BTC 15-min market
            market = discovery.get_active_market()
            
            if not market:
                logging.info("No active BTC 15-min market found. Waiting...")
                time.sleep(10)
                continue
            
            window_count += 1
            logging.info(f"Window {window_count}: {market.window_id} | Ends: {market.end_ts}")
            
            # Trade this window
            result = strategy.trade_window(market, orderbook)
            
            logging.info(
                f"Window complete: SafeProfitNet=${result.safe_profit_net:.4f} | "
                f"Trades={result.trades_count} | Legging={result.legging_events}"
            )
            
            # Update ledger
            ledger.update_session_summary()
            
            if once:
                break
            
            # Wait for next window
            wait_time = max(1, (market.end_ts - datetime.utcnow()).total_seconds() + 5)
            logging.info(f"Waiting {wait_time:.0f}s for next window...")
            
            for _ in range(int(wait_time)):
                if not running:
                    break
                time.sleep(1)
        
        except KeyboardInterrupt:
            break
        
        except Exception as e:
            logging.error(f"Error in paper trading: {e}", exc_info=True)
            metrics.log_error(str(e), {"mode": "paper"})
            time.sleep(5)
    
    # Final summary
    metrics.close()
    print("\n" + "="*60)
    print("FINAL SUMMARY")
    print("="*60)
    stats = ledger.get_session_summary()
    print(f"Windows traded: {window_count}")
    print(f"Total PnL: ${stats.get('total_pnl', 0):.4f}")
    print(f"Total trades: {stats.get('total_trades', 0)}")
    print(f"Legging events: {stats.get('total_legging', 0)}")


def run_record_mode(config: ArbConfig):
    """Record orderbooks for later replay."""
    global running
    
    print("\n" + "="*60)
    print("PM 15m ARB BOT - RECORDING MODE")
    print("="*60)
    print(f"Recording to: {config.recording_dir}")
    print("="*60)
    print("\nPress Ctrl+C to stop.\n")
    
    from pm_15m_arb.market_discovery import BTC15mMarketDiscovery
    from pm_15m_arb.recorder import Recorder
    
    metrics = RecordingMetrics(config)
    discovery = BTC15mMarketDiscovery(config)
    recorder = Recorder(config, metrics)
    
    while running:
        try:
            market = discovery.get_active_market()
            
            if not market:
                logging.info("No active market. Waiting...")
                time.sleep(10)
                continue
            
            logging.info(f"Recording: {market.window_id}")
            recorder.record_window(market)
            
        except KeyboardInterrupt:
            break
        
        except Exception as e:
            logging.error(f"Error in recording: {e}", exc_info=True)
            time.sleep(5)
    
    metrics.close()
    print(f"\nRecordings saved to: {config.recording_dir}")


def run_replay_mode(config: ArbConfig, recording_file: str):
    """Replay from recorded data."""
    print("\n" + "="*60)
    print("PM 15m ARB BOT - REPLAY MODE")
    print("="*60)
    print(f"Replaying: {recording_file}")
    print(f"Seed: {config.replay_seed}")
    print("="*60 + "\n")
    
    from pm_15m_arb.recorder import Replayer
    from pm_15m_arb.strategy import StrategyEngine
    from pm_15m_arb.executor_paper import PaperExecutor
    from pm_15m_arb.ledger import Ledger
    
    # Use separate DB for replay
    config.db_path = "pm_15m_arb_replay.db"
    config.metrics_file = "pm_15m_arb_replay_metrics.jsonl"
    
    metrics = MetricsLogger(config)
    ledger = Ledger(config)
    replayer = Replayer(config, recording_file)
    
    # Create executor and strategy
    from pm_15m_arb.orderbook import OrderbookFetcher
    orderbook = replayer  # Replayer acts as orderbook source
    executor = PaperExecutor(config, orderbook, metrics, ledger)
    strategy = StrategyEngine(config, executor, metrics, ledger)
    
    # Replay all windows in recording
    results = replayer.replay_all(strategy)
    
    # Summary
    metrics.close()
    print("\n" + "="*60)
    print("REPLAY SUMMARY")
    print("="*60)
    print(f"Windows replayed: {len(results)}")
    
    total_pnl = sum(r.safe_profit_net for r in results)
    total_trades = sum(r.trades_count for r in results)
    total_legging = sum(r.legging_events for r in results)
    
    print(f"Total PnL: ${total_pnl:.4f}")
    print(f"Total trades: {total_trades}")
    print(f"Legging events: {total_legging}")
    
    # Verify determinism
    print(f"\nReplay seed: {config.replay_seed}")
    print("Results should be identical across runs with same seed.")


def show_stats(config: ArbConfig):
    """Show statistics."""
    from pm_15m_arb.ledger import Ledger
    
    ledger = Ledger(config)
    stats = ledger.get_stats_summary()
    
    print("\n" + "="*60)
    print("PM 15m ARB - STATISTICS")
    print("="*60)
    
    print(f"\nWindows traded: {stats.get('windows_count', 0)}")
    print(f"Total trades: {stats.get('total_trades', 0)}")
    print(f"Successful pairs: {stats.get('successful_pairs', 0)}")
    print(f"Legging events: {stats.get('legging_events', 0)}")
    
    print(f"\n--- P&L ---")
    print(f"Total PnL: ${stats.get('total_pnl', 0):.4f}")
    print(f"Avg PnL/window: ${stats.get('avg_pnl_per_window', 0):.4f}")
    print(f"Best window: ${stats.get('best_window_pnl', 0):.4f}")
    print(f"Worst window: ${stats.get('worst_window_pnl', 0):.4f}")
    
    print(f"\n--- Edge ---")
    print(f"Avg theoretical edge: {stats.get('avg_theoretical_edge', 0):.4f}")
    print(f"Avg realized edge: {stats.get('avg_realized_edge', 0):.4f}")
    print(f"Edge capture ratio: {stats.get('edge_capture_ratio', 0):.1%}")
    
    print(f"\n--- Slippage ---")
    print(f"Avg slippage: ${stats.get('avg_slippage', 0):.4f}")
    print(f"Max slippage: ${stats.get('max_slippage', 0):.4f}")


def generate_report(config: ArbConfig):
    """Generate markdown report."""
    from pm_15m_arb.report import ReportGenerator
    
    generator = ReportGenerator(config)
    report_path = generator.generate()
    
    print(f"\nReport generated: {report_path}")


def main():
    """Main entry point."""
    parser = argparse.ArgumentParser(
        description="BTC 15-Minute Up/Down Arbitrage Bot (Paper Trading)",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
    python run_pm_15m_arb.py --mode paper              # Paper trade live
    python run_pm_15m_arb.py --mode record             # Record orderbooks
    python run_pm_15m_arb.py --mode replay rec.jsonl   # Replay recording
    python run_pm_15m_arb.py --stats                   # Show statistics
    python run_pm_15m_arb.py --report                  # Generate report

This is PAPER TRADING - no real orders are placed.
        """
    )
    
    parser.add_argument("--mode", choices=["paper", "record", "replay"],
                       default="paper",
                       help="Operating mode (default: paper)")
    parser.add_argument("--once", action="store_true",
                       help="Trade single window and exit (paper mode)")
    parser.add_argument("--stats", action="store_true",
                       help="Show statistics and exit")
    parser.add_argument("--report", action="store_true",
                       help="Generate report and exit")
    parser.add_argument("--config", type=str, default=None,
                       help="Path to config YAML file")
    parser.add_argument("--min-edge", type=float, default=None,
                       help="Override minimum edge threshold")
    parser.add_argument("--poll-interval", type=int, default=None,
                       help="Override poll interval (ms)")
    parser.add_argument("--debug", action="store_true",
                       help="Enable debug logging")
    parser.add_argument("recording_file", nargs="?", default=None,
                       help="Recording file for replay mode")
    
    args = parser.parse_args()
    
    # Load config
    config = load_config(args.config)
    
    # Apply overrides
    if args.min_edge is not None:
        config.min_edge = args.min_edge
    if args.poll_interval is not None:
        config.poll_interval_ms = args.poll_interval
    if args.debug:
        config.log_level = "DEBUG"
    
    # Setup logging
    setup_logging(config)
    
    # Setup signal handlers
    signal.signal(signal.SIGINT, signal_handler)
    if hasattr(signal, 'SIGTERM'):
        signal.signal(signal.SIGTERM, signal_handler)
    
    # Run appropriate mode
    if args.stats:
        show_stats(config)
    elif args.report:
        generate_report(config)
    elif args.mode == "record":
        run_record_mode(config)
    elif args.mode == "replay":
        if not args.recording_file:
            print("ERROR: Replay mode requires a recording file")
            sys.exit(1)
        run_replay_mode(config, args.recording_file)
    else:
        run_paper_mode(config, once=args.once)


if __name__ == "__main__":
    main()

