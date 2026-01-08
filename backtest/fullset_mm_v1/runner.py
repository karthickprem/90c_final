"""CLI runner for full-set MM backtest."""
import argparse
import json
import os
import sys

from .config import (
    StrategyConfig, BacktestConfig,
    DEFAULT_BUY_DIR, DEFAULT_SELL_DIR
)
from .sim import Simulator, run_grid_search
from .report import generate_full_report, write_grid_search_results
from .metrics import find_best_calibration


# Target histogram from @0x8dxd wallet_decoder_v2
TARGET_PAIR_COST_HIST = {
    48: 1, 50: 1, 66: 1, 68: 1,
    72: 999, 74: 3, 76: 1,
    78: 1000, 80: 2, 82: 1,
    84: 999, 86: 1002, 88: 890, 90: 1003, 92: 3,
    94: 1501, 96: 1891, 98: 1001, 100: 1557,
    102: 4446, 104: 500, 106: 3,
    108: 3995, 110: 1499, 112: 1498, 114: 501,
    116: 1002, 118: 499, 120: 1, 122: 1, 126: 1, 134: 1, 142: 1
}


def run_single(args):
    """Run single configuration backtest."""
    strategy_config = StrategyConfig(
        d_cents=args.d,
        chase_step_cents=args.chase_step,
        chase_step_secs=args.chase_step_secs,
        chase_timeout_secs=args.chase_timeout,
        max_pair_cost_cents=args.max_pair_cost,
        fill_model=args.fill_model,
        slip_unwind_cents=args.slip_unwind,
        fee_bps_maker=args.fee_bps_maker,
        maker_rebate_bps=args.rebate_bps
    )
    
    config = BacktestConfig(
        buy_data_dir=args.buy_dir,
        sell_data_dir=args.sell_dir,
        outdir=args.outdir,
        strategy=strategy_config
    )
    
    print("=" * 60)
    print("FULL-SET MM BACKTEST")
    print("=" * 60)
    print(f"\nStrategy Config:")
    print(f"  d (quote offset): {args.d}c")
    print(f"  Chase timeout: {args.chase_timeout}s")
    print(f"  Chase step: {args.chase_step}c / {args.chase_step_secs}s")
    print(f"  Max pair cost: {args.max_pair_cost}c")
    print(f"  Fill model: {args.fill_model}")
    print(f"  Unwind slip: {args.slip_unwind}c")
    print()
    
    sim = Simulator(config)
    result = sim.run()
    
    # Print quick summary
    print("\n" + "=" * 60)
    print("RESULTS SUMMARY")
    print("=" * 60)
    print(f"\nPairs: {result.total_pairs} ({result.profitable_pairs} profitable)")
    print(f"Unwinds: {result.total_unwinds}")
    print(f"Gross edge: {result.gross_edge_cents}c")
    print(f"Unwind losses: {result.unwind_loss_cents}c")
    print(f"Net PnL: {result.net_pnl_cents}c (${result.net_pnl_cents/100:.2f})")
    
    # Generate reports
    target_hist = TARGET_PAIR_COST_HIST if args.compare_target else None
    generate_full_report(result, args.outdir, target_hist)
    
    return result


def run_calibration(args):
    """Run grid search calibration."""
    print("=" * 60)
    print("FULL-SET MM CALIBRATION GRID SEARCH")
    print("=" * 60)
    
    d_values = [int(x) for x in args.d_range.split(',')]
    chase_values = [float(x) for x in args.chase_range.split(',')]
    max_cost_values = [int(x) for x in args.max_cost_range.split(',')]
    
    print(f"\nGrid:")
    print(f"  d: {d_values}")
    print(f"  chase_timeout: {chase_values}")
    print(f"  max_pair_cost: {max_cost_values}")
    print(f"  Total combinations: {len(d_values) * len(chase_values) * len(max_cost_values)}")
    print()
    
    results = run_grid_search(
        args.buy_dir,
        args.sell_dir,
        d_values,
        chase_values,
        max_cost_values,
        fill_model=args.fill_model
    )
    
    # Write grid results
    write_grid_search_results(results, args.outdir, TARGET_PAIR_COST_HIST)
    
    # Find best match
    best_result, best_dist = find_best_calibration(
        results,
        TARGET_PAIR_COST_HIST,
        min_pairs=100
    )
    
    if best_result:
        print("\n" + "=" * 60)
        print("BEST CALIBRATION (by histogram match)")
        print("=" * 60)
        print(f"\nParameters:")
        print(f"  d: {best_result.config.d_cents}c")
        print(f"  chase_timeout: {best_result.config.chase_timeout_secs}s")
        print(f"  max_pair_cost: {best_result.config.max_pair_cost_cents}c")
        print(f"\nResults:")
        print(f"  Pairs: {best_result.total_pairs}")
        print(f"  Net PnL: {best_result.net_pnl_cents}c")
        print(f"  L1 distance: {best_dist.l1_distance:.4f}")
        print(f"  Overlap: {best_dist.overlap_pct:.1f}%")
        
        # Generate full report for best
        best_outdir = os.path.join(args.outdir, "best_calibration")
        generate_full_report(best_result, best_outdir, TARGET_PAIR_COST_HIST)
    
    return results


def main():
    parser = argparse.ArgumentParser(
        description="Full-Set MM Backtest (Ladder + Chase Accumulator)"
    )
    
    # Data paths
    parser.add_argument(
        '--buy-dir', type=str, default=DEFAULT_BUY_DIR,
        help='Path to market_logs (BUY/ASK prices)'
    )
    parser.add_argument(
        '--sell-dir', type=str, default=DEFAULT_SELL_DIR,
        help='Path to market_logs_sell (SELL/BID prices)'
    )
    parser.add_argument(
        '--outdir', type=str, default='out_fullset_mm',
        help='Output directory'
    )
    
    # Mode
    parser.add_argument(
        '--calibrate', action='store_true',
        help='Run grid search calibration'
    )
    
    # Strategy params (for single run)
    parser.add_argument('--d', type=int, default=3, help='Quote offset in cents')
    parser.add_argument('--chase-timeout', type=float, default=15.0, help='Chase timeout (seconds)')
    parser.add_argument('--chase-step', type=int, default=1, help='Chase step (cents)')
    parser.add_argument('--chase-step-secs', type=float, default=2.0, help='Chase step interval (seconds)')
    parser.add_argument('--max-pair-cost', type=int, default=100, help='Max pair cost (cents)')
    parser.add_argument(
        '--fill-model', type=str, default='maker_at_bid',
        choices=['maker_at_bid', 'price_improve_to_ask'],
        help='Fill model'
    )
    parser.add_argument('--slip-unwind', type=int, default=1, help='Unwind slippage (cents)')
    parser.add_argument('--fee-bps-maker', type=float, default=0.0, help='Maker fee (bps)')
    parser.add_argument('--rebate-bps', type=float, default=0.0, help='Maker rebate (bps)')
    
    # Grid search params (for calibration)
    parser.add_argument('--d-range', type=str, default='1,2,3,4,5,6', help='d values (comma-separated)')
    parser.add_argument('--chase-range', type=str, default='5,10,15,20,30', help='Chase timeout values')
    parser.add_argument('--max-cost-range', type=str, default='96,98,100,102', help='Max pair cost values')
    
    # Comparison
    parser.add_argument(
        '--compare-target', action='store_true',
        help='Compare results to @0x8dxd target histogram'
    )
    
    args = parser.parse_args()
    
    # Validate paths
    if not os.path.exists(args.buy_dir):
        print(f"Error: BUY data directory not found: {args.buy_dir}")
        sys.exit(1)
    if not os.path.exists(args.sell_dir):
        print(f"Error: SELL data directory not found: {args.sell_dir}")
        sys.exit(1)
    
    if args.calibrate:
        run_calibration(args)
    else:
        run_single(args)


if __name__ == "__main__":
    main()


