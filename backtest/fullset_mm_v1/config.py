"""Configuration for Full-Set MM Backtest."""
from dataclasses import dataclass, field
from typing import Literal


@dataclass
class StrategyConfig:
    """Strategy parameters for Ladder + Chase Full-Set Accumulator."""
    
    # Quote offset: bid = mid - d
    d_cents: int = 3
    
    # Chase parameters
    chase_step_cents: int = 1
    chase_step_secs: float = 2.0
    chase_timeout_secs: float = 15.0
    
    # Max pair cost (stop chasing if would exceed)
    max_pair_cost_cents: int = 100
    
    # Fill model
    fill_model: Literal["maker_at_bid", "price_improve_to_ask"] = "maker_at_bid"
    
    # Unwind slippage when chase fails
    slip_unwind_cents: int = 1
    
    # Fees (in basis points)
    fee_bps_taker: float = 0.0  # Polymarket has 0 taker fee
    fee_bps_maker: float = 0.0
    maker_rebate_bps: float = 0.0  # Some markets have maker rebates
    
    # Sizing
    size_per_leg: float = 10.0  # dollars per leg


@dataclass
class BacktestConfig:
    """Overall backtest configuration."""
    
    # Data paths
    buy_data_dir: str = ""  # market_logs (ASK prices)
    sell_data_dir: str = ""  # market_logs_sell (BID prices)
    
    # Output directory
    outdir: str = "out_fullset_mm"
    
    # Strategy config
    strategy: StrategyConfig = field(default_factory=StrategyConfig)
    
    # Calibration grid (for grid search)
    calibrate: bool = False
    d_range: list = field(default_factory=lambda: [1, 2, 3, 4, 5, 6])
    chase_timeout_range: list = field(default_factory=lambda: [5, 10, 15, 20, 30])
    max_pair_cost_range: list = field(default_factory=lambda: [96, 98, 100, 102])
    
    # Target histogram for calibration (from wallet_decoder_v2)
    target_pair_cost_hist: dict = field(default_factory=dict)


# Default paths for the cloned repo
DEFAULT_BUY_DIR = r"C:\Users\karthick\Documents\tmp\backtesting15mbitcoin\market_logs"
DEFAULT_SELL_DIR = r"C:\Users\karthick\Documents\tmp\backtesting15mbitcoin\market_logs_sell"


