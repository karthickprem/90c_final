"""
Configuration for PM 15-Minute Arbitrage Bot.

All tunable knobs are centralized here.
Key insight: Trading fees = 0 on Polymarket, but slippage is real.
"""

from dataclasses import dataclass, field
from typing import Optional, List
import yaml
from pathlib import Path


@dataclass
class ArbConfig:
    """
    Configuration for BTC 15-minute Up/Down arbitrage bot.
    
    Conservative defaults - start with buffers and tighten after 
    observing real slippage from paper trading.
    """
    
    # === API Endpoints ===
    gamma_api_url: str = "https://gamma-api.polymarket.com"
    clob_api_url: str = "https://clob.polymarket.com"
    
    # === Polling ===
    poll_interval_ms: int = 500  # How often to poll orderbooks (250-1000ms)
    
    # === Fee and Slippage Buffers ===
    # Polymarket trading fees = 0, but we model slippage
    fee_buffer_per_share: float = 0.0  # Platform fee (currently 0)
    slippage_buffer_per_leg: float = 0.002  # 0.2% slippage buffer per leg (conservative)
    
    # === Signal Thresholds ===
    min_edge: float = 0.015  # 1.5% minimum edge after buffers (conservative start)
    min_depth_shares: float = 50.0  # Minimum size at best ask for each side
    
    # === Risk Limits ===
    max_notional_per_window: float = 100.0  # Max USD per 15-min window
    max_total_notional: float = 500.0  # Max total exposure
    target_profit: float = 0.50  # Stop trading window when SafeProfitNet >= this
    order_size_usd: float = 10.0  # Default order size per trade
    
    # === Legging Protection ===
    max_leg_timeout_ms: int = 2000  # Max time to wait for second leg (ms)
    max_leg_slippage: float = 0.01  # Max slippage to accept when completing missing leg (1%)
    max_unwind_loss_pct: float = 0.005  # Max loss % when unwinding failed leg (0.5%)
    
    # === Time Cutoffs ===
    stop_add_seconds_before_end: int = 30  # Stop new trades N seconds before window end
    
    # === Variant B Overlay ===
    enable_overlay_b: bool = False  # Keep OFF by default
    min_improvement_b: float = 0.005  # Min improvement for overlay B trades
    overlay_b_never_negative: bool = True  # Never let SafeProfitNet go negative
    
    # === Market Discovery ===
    btc_market_keywords: List[str] = field(default_factory=lambda: [
        "bitcoin", "btc", "15-minute", "15 minute", "15min",
        "up or down", "up/down", "higher or lower"
    ])
    
    # === Database and Logging ===
    db_path: str = "pm_15m_arb.db"
    log_level: str = "INFO"
    log_file: str = "pm_15m_arb.log"
    metrics_file: str = "pm_15m_arb_metrics.jsonl"
    recording_dir: str = "pm_15m_recordings"
    
    # === Replay ===
    replay_speed: float = 1.0  # Replay speed multiplier (1.0 = real time)
    replay_seed: int = 42  # Random seed for deterministic replay
    
    # === Safety Switches (for live trading later) ===
    max_daily_loss: float = 50.0  # Hard stop if daily loss exceeds this
    kill_switch_file: str = "KILL_SWITCH"  # If this file exists, stop immediately
    
    @property
    def total_buffer_per_pair(self) -> float:
        """Total buffer to subtract from 1.0 when checking pair cost."""
        return 2 * (self.fee_buffer_per_share + self.slippage_buffer_per_leg)
    
    @property
    def pair_cost_threshold(self) -> float:
        """Maximum allowed pair cost: 1 - min_edge - buffers."""
        return 1.0 - self.min_edge - self.total_buffer_per_pair
    
    @property
    def poll_interval_seconds(self) -> float:
        """Poll interval in seconds."""
        return self.poll_interval_ms / 1000.0
    
    @classmethod
    def from_yaml(cls, path: str) -> "ArbConfig":
        """Load config from YAML file."""
        with open(path, "r") as f:
            data = yaml.safe_load(f) or {}
        return cls(**{k: v for k, v in data.items() if hasattr(cls, k)})
    
    def to_yaml(self, path: str):
        """Save config to YAML file."""
        data = {
            "gamma_api_url": self.gamma_api_url,
            "clob_api_url": self.clob_api_url,
            "poll_interval_ms": self.poll_interval_ms,
            "fee_buffer_per_share": self.fee_buffer_per_share,
            "slippage_buffer_per_leg": self.slippage_buffer_per_leg,
            "min_edge": self.min_edge,
            "min_depth_shares": self.min_depth_shares,
            "max_notional_per_window": self.max_notional_per_window,
            "max_total_notional": self.max_total_notional,
            "target_profit": self.target_profit,
            "order_size_usd": self.order_size_usd,
            "max_leg_timeout_ms": self.max_leg_timeout_ms,
            "max_leg_slippage": self.max_leg_slippage,
            "max_unwind_loss_pct": self.max_unwind_loss_pct,
            "stop_add_seconds_before_end": self.stop_add_seconds_before_end,
            "enable_overlay_b": self.enable_overlay_b,
            "min_improvement_b": self.min_improvement_b,
            "overlay_b_never_negative": self.overlay_b_never_negative,
            "db_path": self.db_path,
            "log_level": self.log_level,
            "log_file": self.log_file,
            "metrics_file": self.metrics_file,
            "recording_dir": self.recording_dir,
            "replay_speed": self.replay_speed,
            "replay_seed": self.replay_seed,
            "max_daily_loss": self.max_daily_loss,
            "kill_switch_file": self.kill_switch_file,
        }
        with open(path, "w") as f:
            yaml.dump(data, f, default_flow_style=False, sort_keys=False)
    
    def print_summary(self):
        """Print config summary for logging."""
        print("\n=== PM 15m Arb Config ===")
        print(f"Poll interval: {self.poll_interval_ms}ms")
        print(f"Min edge: {self.min_edge:.3f} ({self.min_edge*100:.1f}%)")
        print(f"Slippage buffer/leg: {self.slippage_buffer_per_leg:.4f}")
        print(f"Total buffer/pair: {self.total_buffer_per_pair:.4f}")
        print(f"Pair cost threshold: {self.pair_cost_threshold:.4f}")
        print(f"Min depth: {self.min_depth_shares} shares")
        print(f"Order size: ${self.order_size_usd}")
        print(f"Target profit: ${self.target_profit}")
        print(f"Stop before end: {self.stop_add_seconds_before_end}s")
        print(f"Overlay B: {'ENABLED' if self.enable_overlay_b else 'DISABLED'}")
        print("=========================\n")


# Default config instance
DEFAULT_CONFIG = ArbConfig()


def load_config(path: Optional[str] = None) -> ArbConfig:
    """Load config from file or return default."""
    if path and Path(path).exists():
        return ArbConfig.from_yaml(path)
    return ArbConfig()


if __name__ == "__main__":
    # Test config
    config = ArbConfig()
    config.print_summary()
    
    # Save sample config
    config.to_yaml("pm_15m_arb_config.yaml")
    print("Saved sample config to pm_15m_arb_config.yaml")

