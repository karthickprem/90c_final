"""
Configuration for Wallet Decoder
"""

from dataclasses import dataclass, field
from typing import Optional
from datetime import datetime


# API endpoints
DATA_API_BASE = "https://data-api.polymarket.com"
TRADES_ENDPOINT = f"{DATA_API_BASE}/trades"
ACTIVITY_ENDPOINT = f"{DATA_API_BASE}/activity"

# Request settings
DEFAULT_LIMIT = 500
DEFAULT_TIMEOUT = 10.0
MAX_RETRIES = 5
RETRY_BACKOFF_BASE = 2.0  # Exponential backoff base

# Episode detection
EPISODE_GAP_MINUTES = 10  # Gap to start new episode

# Classification thresholds
FULL_SET_MATCH_RATIO = 0.7  # matched/max >= this
FULL_SET_MIN_EDGE = 0.002   # 0.2c minimum edge
MERGE_HORIZON_MINUTES = 30   # Merge within this time
MM_MIN_TRADES_PER_DAY = 50   # Market maker threshold
MM_MAX_INVENTORY_RATIO = 0.3  # Max inventory imbalance


@dataclass
class DecoderConfig:
    """Runtime configuration for decoder."""
    user_address: str
    outdir: str = "out_wallet"
    
    # Date filters
    start_date: Optional[datetime] = None
    end_date: Optional[datetime] = None
    
    # Pagination
    limit: int = DEFAULT_LIMIT
    max_pages: int = 1000  # Safety limit
    
    # PnL
    fee_bps: float = 0.0  # Fee estimate in basis points
    
    # Runtime
    verbose: bool = False
    
    def __post_init__(self):
        # Normalize address
        self.user_address = self.user_address.lower()


