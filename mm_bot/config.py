"""
Configuration for Market Making Bot
====================================
Typed config with env parsing and sensible defaults for small account (~$26).
"""

import os
import json
from dataclasses import dataclass, field
from typing import Optional
from enum import Enum


class RunMode(Enum):
    DRYRUN = "dryrun"   # No orders, just log what would happen
    PAPER = "paper"     # Simulated fills
    LIVE = "live"       # Real orders


@dataclass
class RiskLimits:
    """Conservative limits for small account"""
    # Maximum USDC locked in open buy orders
    max_usdc_locked: float = 10.0
    
    # Maximum shares per token (YES or NO)
    max_inv_shares_per_token: float = 50.0
    
    # Maximum open orders per token per side (1 bid + 1 ask max)
    max_orders_per_token_side: int = 1
    
    # Maximum order replacements per minute (throttle)
    max_replace_per_min: int = 20
    
    # Kill switch: cancel all if inventory exceeds this
    kill_switch_inv_threshold: float = 100.0
    
    # Kill switch: cancel all if unrealized loss exceeds this
    kill_switch_loss_threshold: float = 5.0
    
    # Minimum time between order updates (seconds)
    min_update_interval: float = 3.0
    
    # Stop-loss: exit if price moves against by this many cents
    stop_loss_cents: float = 3.0
    
    # Emergency taker exit: allow crossing spread in emergencies
    emergency_taker_exit: bool = False
    
    # MIN SIZE + DUST MODE (Fix #2)
    # Minimum order size (Polymarket requires 5 shares minimum)
    min_order_size: float = 5.0
    
    # Buffer above min size to avoid dust positions
    min_order_size_buffer: float = 1.0  # So actual min = 6 shares
    
    # Dust mode: what to do if shares < min_order_size
    # "TOPUP" = buy more to reach min, "HOLD" = hold to settlement
    dust_mode: str = "HOLD"


@dataclass
class QuotingParams:
    """Parameters for quote generation"""
    # Minimum spread (half-spread on each side)
    min_half_spread_cents: float = 1.0  # 1 cent minimum
    
    # Target spread (will widen under stress)
    target_half_spread_cents: float = 2.0
    
    # Inventory skew factor (how much to skew quotes based on inventory)
    # 0 = no skew, 1 = aggressive skew
    inventory_skew_factor: float = 0.5
    
    # Size per quote (shares)
    base_quote_size: float = 10.0
    
    # Tick size (Polymarket uses 0.01 = 1 cent)
    tick_size: float = 0.01
    
    # Price bounds
    min_price: float = 0.01
    max_price: float = 0.99
    
    # Edge requirement (minimum expected profit per trade in cents)
    min_edge_cents: float = 0.5
    
    # Spike detection
    spike_threshold_cents: float = 2.0  # Price move to trigger spike
    spike_window_secs: float = 5.0      # Time window to measure spike
    spike_cooldown_secs: float = 10.0   # Pause duration after spike


@dataclass
class ApiConfig:
    """API credentials and endpoints"""
    api_key: str = ""
    api_secret: str = ""
    api_passphrase: str = ""
    private_key: str = ""
    proxy_address: str = ""  # Polymarket Proxy Wallet address
    signature_type: int = 1  # For proxy wallet
    
    clob_host: str = "https://clob.polymarket.com"
    gamma_host: str = "https://gamma-api.polymarket.com"
    ws_host: str = "wss://ws-subscriptions-clob.polymarket.com/ws/user"
    
    chain_id: int = 137  # Polygon


@dataclass
class MarketConfig:
    """BTC 15-min market configuration"""
    # If token IDs are known, use them directly (more reliable)
    yes_token_id: Optional[str] = None
    no_token_id: Optional[str] = None
    
    # Fallback: resolve by slug pattern
    slug_pattern: str = "btc-updown-15m"
    
    # Poll interval for market data (seconds)
    poll_interval: float = 1.0


@dataclass
class Config:
    """Master configuration"""
    mode: RunMode = RunMode.DRYRUN
    risk: RiskLimits = field(default_factory=RiskLimits)
    quoting: QuotingParams = field(default_factory=QuotingParams)
    api: ApiConfig = field(default_factory=ApiConfig)
    market: MarketConfig = field(default_factory=MarketConfig)
    
    # Logging
    log_file: str = "mm_bot.jsonl"
    verbose: bool = True
    
    @classmethod
    def from_env(cls, config_file: str = "pm_api_config.json") -> "Config":
        """Load config from environment and config file"""
        cfg = cls()
        
        # Determine run mode
        live_env = os.environ.get("LIVE", "0")
        if live_env == "1":
            cfg.mode = RunMode.LIVE
        elif os.environ.get("PAPER", "0") == "1":
            cfg.mode = RunMode.PAPER
        else:
            cfg.mode = RunMode.DRYRUN
        
        # Load API credentials from file
        if os.path.exists(config_file):
            with open(config_file) as f:
                api_cfg = json.load(f)
            
            cfg.api.api_key = api_cfg.get("api_key", "")
            cfg.api.api_secret = api_cfg.get("api_secret", "")
            cfg.api.api_passphrase = api_cfg.get("api_passphrase", "")
            cfg.api.private_key = api_cfg.get("private_key", "")
            cfg.api.proxy_address = api_cfg.get("proxy_address", "")
            cfg.api.signature_type = api_cfg.get("signature_type", 1)
        
        # Override from env if set
        if os.environ.get("PM_API_KEY"):
            cfg.api.api_key = os.environ["PM_API_KEY"]
        if os.environ.get("PM_PROXY"):
            cfg.api.proxy_address = os.environ["PM_PROXY"]
        
        # Risk overrides from env
        if os.environ.get("MAX_USDC") or os.environ.get("MM_MAX_USDC_LOCKED"):
            cfg.risk.max_usdc_locked = float(os.environ.get("MM_MAX_USDC_LOCKED") or os.environ.get("MAX_USDC"))
        if os.environ.get("MAX_SHARES") or os.environ.get("MM_MAX_SHARES_PER_TOKEN"):
            cfg.risk.max_inv_shares_per_token = float(os.environ.get("MM_MAX_SHARES_PER_TOKEN") or os.environ.get("MAX_SHARES"))
        
        # Quoting overrides from env
        if os.environ.get("MM_MIN_SPREAD"):
            # Convert cents to decimal (e.g., 0.01 = 1 cent half-spread)
            val = float(os.environ["MM_MIN_SPREAD"])
            cfg.quoting.min_half_spread_cents = val * 100 if val < 1 else val
        if os.environ.get("MM_QUOTE_SIZE"):
            cfg.quoting.base_quote_size = float(os.environ["MM_QUOTE_SIZE"])
        if os.environ.get("MM_TARGET_SPREAD"):
            val = float(os.environ["MM_TARGET_SPREAD"])
            cfg.quoting.target_half_spread_cents = val * 100 if val < 1 else val
        
        # Stop-loss and emergency exit from env
        if os.environ.get("MM_STOP_LOSS_CENTS"):
            cfg.risk.stop_loss_cents = float(os.environ["MM_STOP_LOSS_CENTS"])
        if os.environ.get("MM_EMERGENCY_TAKER_EXIT"):
            cfg.risk.emergency_taker_exit = os.environ["MM_EMERGENCY_TAKER_EXIT"] == "1"
        
        # Spike detection from env
        if os.environ.get("MM_SPIKE_THRESHOLD_CENTS"):
            cfg.quoting.spike_threshold_cents = float(os.environ["MM_SPIKE_THRESHOLD_CENTS"])
        if os.environ.get("MM_SPIKE_COOLDOWN_SECS"):
            cfg.quoting.spike_cooldown_secs = float(os.environ["MM_SPIKE_COOLDOWN_SECS"])
        
        # Min order size from env
        if os.environ.get("MM_MIN_ORDER_SIZE"):
            cfg.risk.min_order_size = float(os.environ["MM_MIN_ORDER_SIZE"])
        if os.environ.get("MM_MIN_ORDER_BUFFER"):
            cfg.risk.min_order_size_buffer = float(os.environ["MM_MIN_ORDER_BUFFER"])
        if os.environ.get("MM_DUST_MODE"):
            cfg.risk.dust_mode = os.environ["MM_DUST_MODE"].upper()
        
        return cfg
    
    def validate(self) -> list[str]:
        """Validate configuration, return list of errors"""
        errors = []
        
        if self.mode == RunMode.LIVE:
            if not self.api.api_key:
                errors.append("LIVE mode requires api_key")
            if not self.api.api_secret:
                errors.append("LIVE mode requires api_secret")
            if not self.api.private_key:
                errors.append("LIVE mode requires private_key")
            if not self.api.proxy_address:
                errors.append("LIVE mode requires proxy_address")
        
        if self.risk.max_usdc_locked <= 0:
            errors.append("max_usdc_locked must be positive")
        
        if self.quoting.min_half_spread_cents < 0.5:
            errors.append("min_half_spread_cents too small (risk of crossing)")
        
        return errors
    
    def print_summary(self):
        """Print configuration summary"""
        print("=" * 60)
        print("MARKET MAKING BOT CONFIGURATION")
        print("=" * 60)
        print(f"Mode:           {self.mode.value.upper()}")
        print(f"Proxy Wallet:   {self.api.proxy_address[:20]}..." if self.api.proxy_address else "Proxy Wallet:   NOT SET")
        print()
        print("Risk Limits:")
        print(f"  Max USDC Locked:    ${self.risk.max_usdc_locked:.2f}")
        print(f"  Max Shares/Token:   {self.risk.max_inv_shares_per_token:.0f}")
        print(f"  Max Orders/Side:    {self.risk.max_orders_per_token_side}")
        print(f"  Max Replaces/Min:   {self.risk.max_replace_per_min}")
        print()
        print("Quoting:")
        print(f"  Min Half-Spread:    {self.quoting.min_half_spread_cents:.1f}c")
        print(f"  Target Half-Spread: {self.quoting.target_half_spread_cents:.1f}c")
        print(f"  Base Quote Size:    {self.quoting.base_quote_size:.0f} shares")
        print(f"  Inventory Skew:     {self.quoting.inventory_skew_factor:.2f}")
        print("=" * 60)

