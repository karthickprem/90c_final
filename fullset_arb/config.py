"""
Configuration for Full-Set Arbitrage Bot.

Key parameters:
- min_edge: Minimum edge (1 - askYES - askNO) to consider
- max_spread_each: Maximum bid-ask spread per side
- min_top_depth: Minimum size at best ask
- max_fill_ms: Max time to fill both legs (simulated)
- max_unwind_loss: Max loss % before disabling market
"""

from dataclasses import dataclass, field
from typing import Optional
import yaml
from pathlib import Path


@dataclass
class ArbConfig:
    """Configuration for full-set arbitrage bot."""
    
    # === API Endpoints ===
    gamma_api_url: str = "https://gamma-api.polymarket.com"
    clob_api_url: str = "https://clob.polymarket.com"
    
    # === Market Discovery ===
    # Only scan markets with these characteristics
    min_volume_24h: float = 1000.0  # Minimum 24h volume in USDC
    min_liquidity: float = 500.0    # Minimum total liquidity
    max_markets_to_scan: int = 200  # Max markets to scan per cycle
    market_types: list = field(default_factory=lambda: ["binary"])  # Only binary for now
    
    # === Arbitrage Thresholds ===
    # Core signal: edge_buy = 1.0 - (askYES + askNO)
    min_edge: float = 0.006         # 0.6% minimum edge after buffers
    min_edge_after_fees: float = 0.003  # 0.3% after fees
    
    # Spread and depth requirements
    max_spread_each: float = 0.02   # 2 cents max spread per side
    min_top_depth: float = 50.0     # Min shares at best ask for each side
    
    # === Execution Parameters ===
    # Simulated paired IOC (immediate-or-cancel) behavior
    max_fill_ms: int = 200          # Max time to fill both legs (ms)
    order_size_usd: float = 10.0    # Size per opportunity (paper)
    max_slippage_pct: float = 0.01  # 1% max slippage from best ask
    
    # === Risk Management ===
    # One-leg risk: if only one leg fills, we must unwind
    max_unwind_loss_pct: float = 0.002  # 0.2% max loss on unwind
    market_disable_minutes: int = 5     # Disable market after bad unwind
    max_daily_loss_usd: float = 50.0    # Stop trading if daily loss exceeds
    max_open_positions: int = 10        # Max concurrent open positions
    
    # === Fees ===
    # Polymarket fee schedule (currently 0, but may change)
    taker_fee_bps: float = 0.0      # Taker fee in basis points
    maker_fee_bps: float = 0.0      # Maker fee in basis points
    
    # === Scan Behavior ===
    scan_interval_seconds: float = 2.0  # How often to scan for opportunities
    log_all_opportunities: bool = True  # Log even non-actionable opps
    
    # === Database ===
    db_path: str = "fullset_arb.db"
    
    # === Logging ===
    log_level: str = "INFO"
    log_file: str = "fullset_arb.log"
    metrics_file: str = "fullset_arb_metrics.jsonl"
    
    @property
    def taker_fee_fraction(self) -> float:
        """Convert bps to fraction."""
        return self.taker_fee_bps / 10000.0
    
    @property
    def effective_min_edge(self) -> float:
        """Minimum edge accounting for fees on both legs."""
        # Buying both YES and NO means 2x taker fees
        total_fee = 2 * self.taker_fee_fraction
        return self.min_edge + total_fee
    
    @classmethod
    def from_yaml(cls, path: str) -> "ArbConfig":
        """Load config from YAML file."""
        with open(path, "r") as f:
            data = yaml.safe_load(f)
        return cls(**{k: v for k, v in data.items() if hasattr(cls, k)})
    
    def to_yaml(self, path: str):
        """Save config to YAML file."""
        data = {
            "gamma_api_url": self.gamma_api_url,
            "clob_api_url": self.clob_api_url,
            "min_volume_24h": self.min_volume_24h,
            "min_liquidity": self.min_liquidity,
            "max_markets_to_scan": self.max_markets_to_scan,
            "min_edge": self.min_edge,
            "min_edge_after_fees": self.min_edge_after_fees,
            "max_spread_each": self.max_spread_each,
            "min_top_depth": self.min_top_depth,
            "max_fill_ms": self.max_fill_ms,
            "order_size_usd": self.order_size_usd,
            "max_slippage_pct": self.max_slippage_pct,
            "max_unwind_loss_pct": self.max_unwind_loss_pct,
            "market_disable_minutes": self.market_disable_minutes,
            "max_daily_loss_usd": self.max_daily_loss_usd,
            "max_open_positions": self.max_open_positions,
            "taker_fee_bps": self.taker_fee_bps,
            "maker_fee_bps": self.maker_fee_bps,
            "scan_interval_seconds": self.scan_interval_seconds,
            "log_all_opportunities": self.log_all_opportunities,
            "db_path": self.db_path,
            "log_level": self.log_level,
            "log_file": self.log_file,
            "metrics_file": self.metrics_file,
        }
        with open(path, "w") as f:
            yaml.dump(data, f, default_flow_style=False)


# Default config instance
DEFAULT_CONFIG = ArbConfig()


def load_config(path: Optional[str] = None) -> ArbConfig:
    """Load config from file or return default."""
    if path and Path(path).exists():
        return ArbConfig.from_yaml(path)
    return ArbConfig()





