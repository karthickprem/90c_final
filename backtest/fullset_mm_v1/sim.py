"""Simulation engine for full-set accumulator backtest."""
from dataclasses import dataclass, field
from typing import List, Dict, Any
import os

from .config import StrategyConfig, BacktestConfig
from .stream import WindowData, load_all_windows
from .strategy import FullSetAccumulator, StrategyState, CompletedPair, UnwindEvent


@dataclass
class SimulationResult:
    """Complete results from a simulation run."""
    config: StrategyConfig
    
    # Aggregated results
    windows_processed: int = 0
    windows_with_activity: int = 0
    
    # All pairs and unwinds
    all_pairs: List[CompletedPair] = field(default_factory=list)
    all_unwinds: List[UnwindEvent] = field(default_factory=list)
    
    # Summary stats
    total_pairs: int = 0
    profitable_pairs: int = 0
    chase_completed_pairs: int = 0
    total_unwinds: int = 0
    
    gross_edge_cents: int = 0
    unwind_loss_cents: int = 0
    net_pnl_cents: int = 0
    
    # Histograms
    pair_cost_hist: Dict[int, int] = field(default_factory=dict)
    dt_hist: Dict[int, int] = field(default_factory=dict)


class Simulator:
    """Run the full-set accumulator simulation."""
    
    def __init__(self, config: BacktestConfig):
        self.config = config
    
    def run(self, windows: List[WindowData] = None) -> SimulationResult:
        """Run simulation on all windows.
        
        If windows not provided, loads from config paths.
        """
        if windows is None:
            print(f"Loading windows from {self.config.buy_data_dir}...")
            windows = load_all_windows(
                self.config.buy_data_dir,
                self.config.sell_data_dir
            )
            print(f"Loaded {len(windows)} windows with valid tick data")
        
        strategy = FullSetAccumulator(self.config.strategy)
        result = SimulationResult(config=self.config.strategy)
        result.windows_processed = len(windows)
        
        for i, window in enumerate(windows):
            if i % 500 == 0:
                print(f"Processing window {i}/{len(windows)}: {window.window_id}")
            
            state = strategy.run_window(window)
            
            # Collect results
            if state.completed_pairs or state.unwind_events:
                result.windows_with_activity += 1
            
            result.all_pairs.extend(state.completed_pairs)
            result.all_unwinds.extend(state.unwind_events)
        
        # Compute summary stats
        self._compute_stats(result)
        
        return result
    
    def _compute_stats(self, result: SimulationResult):
        """Compute summary statistics from raw results."""
        result.total_pairs = len(result.all_pairs)
        result.profitable_pairs = sum(1 for p in result.all_pairs if p.edge_cents > 0)
        result.chase_completed_pairs = sum(1 for p in result.all_pairs if p.completed_via_chase)
        result.total_unwinds = len(result.all_unwinds)
        
        result.gross_edge_cents = sum(p.edge_cents for p in result.all_pairs)
        result.unwind_loss_cents = sum(u.pnl_cents for u in result.all_unwinds)  # Usually negative
        result.net_pnl_cents = result.gross_edge_cents + result.unwind_loss_cents
        
        # Build histograms
        for pair in result.all_pairs:
            # Pair cost histogram (2c buckets)
            bucket = (pair.pair_cost // 2) * 2
            result.pair_cost_hist[bucket] = result.pair_cost_hist.get(bucket, 0) + 1
            
            # DT histogram (5s buckets)
            dt_bucket = int(pair.dt_between_legs // 5) * 5
            result.dt_hist[dt_bucket] = result.dt_hist.get(dt_bucket, 0) + 1


def run_single_config(
    buy_dir: str,
    sell_dir: str,
    strategy_config: StrategyConfig
) -> SimulationResult:
    """Convenience function to run a single configuration."""
    config = BacktestConfig(
        buy_data_dir=buy_dir,
        sell_data_dir=sell_dir,
        strategy=strategy_config
    )
    sim = Simulator(config)
    return sim.run()


def run_grid_search(
    buy_dir: str,
    sell_dir: str,
    d_values: List[int],
    chase_timeout_values: List[float],
    max_pair_cost_values: List[int],
    fill_model: str = "maker_at_bid"
) -> List[SimulationResult]:
    """Run grid search over parameter combinations."""
    # Load windows once
    print("Loading windows for grid search...")
    windows = load_all_windows(buy_dir, sell_dir)
    print(f"Loaded {len(windows)} windows")
    
    results = []
    total_combos = len(d_values) * len(chase_timeout_values) * len(max_pair_cost_values)
    combo_idx = 0
    
    for d in d_values:
        for chase_timeout in chase_timeout_values:
            for max_pair_cost in max_pair_cost_values:
                combo_idx += 1
                print(f"\n=== Grid {combo_idx}/{total_combos}: d={d}, chase={chase_timeout}s, max_cost={max_pair_cost}c ===")
                
                strategy_config = StrategyConfig(
                    d_cents=d,
                    chase_timeout_secs=chase_timeout,
                    max_pair_cost_cents=max_pair_cost,
                    fill_model=fill_model
                )
                
                config = BacktestConfig(
                    buy_data_dir=buy_dir,
                    sell_data_dir=sell_dir,
                    strategy=strategy_config
                )
                
                sim = Simulator(config)
                result = sim.run(windows)
                results.append(result)
                
                # Print quick summary
                print(f"  Pairs: {result.total_pairs}, Profitable: {result.profitable_pairs}, "
                      f"Net PnL: {result.net_pnl_cents}c")
    
    return results


