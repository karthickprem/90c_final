"""
Configuration for Wallet Decoder V2
"""

from dataclasses import dataclass, field
from typing import Optional, List
from datetime import datetime


# API endpoints
DATA_API_BASE = "https://data-api.polymarket.com"
CLOB_API_BASE = "https://clob.polymarket.com"
TRADES_ENDPOINT = f"{DATA_API_BASE}/trades"
ACTIVITY_ENDPOINT = f"{DATA_API_BASE}/activity"

# Request settings
DEFAULT_LIMIT = 500
DEFAULT_TIMEOUT = 10.0
MAX_RETRIES = 5
RETRY_BACKOFF_BASE = 2.0

# Pairing engine
PAIR_WINDOW_SECS = 30.0         # Max seconds between paired buys
FULLSET_COST_BUFFER = 0.03     # Pair cost must be < 1.0 - buffer to count as "edge"

# Fee model (Polymarket 15m markets)
DEFAULT_TAKER_FEE_BPS = 200     # 2% taker fee (adjustable)
DEFAULT_MAKER_REBATE_BPS = 50   # 0.5% maker rebate (estimate)

# Maker/taker inference
MAKER_PRICE_TOLERANCE = 0.005  # If buy price < ask - tolerance => likely maker


@dataclass
class DecoderV2Config:
    """Runtime configuration for V2 decoder."""
    user_address: str
    outdir: str = "out_wallet_v2"
    
    # Date filters
    start_date: Optional[datetime] = None
    end_date: Optional[datetime] = None
    
    # Pagination
    limit: int = DEFAULT_LIMIT
    max_pages: int = 1000
    
    # Fee model
    taker_fee_bps: float = DEFAULT_TAKER_FEE_BPS
    maker_rebate_bps: float = DEFAULT_MAKER_REBATE_BPS
    
    # Pairing
    pair_window_secs: float = PAIR_WINDOW_SECS
    fullset_buffer: float = FULLSET_COST_BUFFER
    
    # Runtime
    verbose: bool = False
    
    def __post_init__(self):
        self.user_address = self.user_address.lower()
    
    @property
    def taker_fee_rate(self) -> float:
        return self.taker_fee_bps / 10000
    
    @property
    def maker_rebate_rate(self) -> float:
        return self.maker_rebate_bps / 10000


