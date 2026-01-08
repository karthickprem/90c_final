"""
Configuration for Paper Mode

All thresholds match FINAL PRODUCTION CONFIG exactly.
"""

from dataclasses import dataclass
from enum import Enum


class QuoteMode(Enum):
    """Price quote mode for paper trading."""
    BIDASK = "bidask"  # Use real bid/ask from orderbook (required for realistic simulation)
    MID = "mid"        # Use midpoint only (NOT decision-grade for deployment)


@dataclass
class StrategyConfig:
    """Strategy configuration matching final production spec."""
    
    # Trigger
    trigger_threshold: int = 90  # First touch >= 90c
    
    # SPIKE Validation (over 10s window, chosen side only)
    spike_min: int = 88  # min_side >= 88c
    spike_max: int = 93  # max_side >= 93c
    validation_secs: float = 10.0  # Validation window
    
    # JUMP Gate (both sides over 10s window)
    big_jump: int = 8  # max(|delta|) < 8c
    mid_jump: int = 3  # count(|delta| >= 3c)
    max_mid_count: int = 2  # < 2 mid jumps
    
    # Execution
    p_max: int = 93  # Limit buy cap
    fill_timeout_secs: float = 2.0  # Cancel if not filled
    slip_entry: int = 1  # Entry slippage against us
    slip_exit: int = 1  # Exit slippage against us
    
    # Exits
    tp: int = 97  # Take profit
    sl: int = 86  # Stop loss
    
    # Sizing
    f: float = 0.02  # Fraction of bankroll per trade


@dataclass
class PaperConfig:
    """Paper mode runtime configuration."""
    
    # Polling
    poll_interval_secs: float = 1.0
    
    # Scheduler resync settings
    gap_resync_threshold: float = 3.0  # Resync if gap > N * poll_interval
    
    # Bankroll
    starting_bankroll: float = 100.0
    
    # Output
    outdir: str = "out_paper"
    
    # Quote mode (CRITICAL for realistic simulation)
    quote_mode: QuoteMode = QuoteMode.BIDASK  # Default to realistic bid/ask
    allow_mid: bool = False  # Must be True to allow MID mode
    synthetic_spread: int = 2  # Default spread for synthetic quotes (cents)
    
    # Strategy
    strategy: StrategyConfig = None
    
    def __post_init__(self):
        if self.strategy is None:
            self.strategy = StrategyConfig()
        
        # HARD GUARD: Refuse to run in mid mode unless explicitly allowed
        if self.quote_mode == QuoteMode.MID and not self.allow_mid:
            raise ValueError(
                "REFUSED: MID quote mode is not decision-grade for deployment. "
                "Use --quote-mode bidask (default) or pass --allow-mid to override."
            )

