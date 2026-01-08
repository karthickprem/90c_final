"""
Normalization - Convert raw API data to unified Event model
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import List, Dict, Any, Optional, Literal
import re


EventKind = Literal["TRADE", "MERGE", "SPLIT", "REDEEM", "REWARD", "TRANSFER", "UNKNOWN"]
OutcomeType = Literal["UP", "DOWN", "YES", "NO", "A", "B", None]
SideType = Literal["BUY", "SELL", None]


@dataclass
class Event:
    """Unified event model for trades and activity."""
    ts: datetime
    kind: EventKind
    market_id: Optional[str] = None       # conditionId or market slug/id
    window_id: Optional[str] = None       # e.g., btc-updown-15m-<start>
    outcome: OutcomeType = None           # UP/DOWN/YES/NO
    side: SideType = None                 # BUY/SELL
    price: Optional[float] = None         # 0.00..1.00
    size: Optional[float] = None          # shares
    cash_delta: Optional[float] = None    # signed cash change
    tx: Optional[str] = None              # transaction hash
    meta: Dict[str, Any] = field(default_factory=dict)  # original fields
    
    def __post_init__(self):
        # Ensure meta is a dict
        if self.meta is None:
            self.meta = {}


def parse_timestamp(val: Any) -> Optional[datetime]:
    """Parse various timestamp formats."""
    if val is None:
        return None
    
    try:
        if isinstance(val, datetime):
            return val
        
        if isinstance(val, (int, float)):
            # Unix timestamp (seconds or milliseconds)
            if val > 1e12:
                val = val / 1000  # milliseconds to seconds
            return datetime.fromtimestamp(val)
        
        # ISO string
        ts_str = str(val).replace("Z", "").split("+")[0].split(".")[0]
        return datetime.fromisoformat(ts_str)
    except:
        return None


def parse_price(val: Any) -> Optional[float]:
    """Parse price to 0..1 float."""
    if val is None:
        return None
    
    try:
        p = float(val)
        
        # Normalize to 0..1
        if p > 1.0:
            # Probably in cents (0-100)
            p = p / 100.0
        
        return max(0.0, min(1.0, p))
    except:
        return None


def parse_size(val: Any) -> Optional[float]:
    """Parse size to float."""
    if val is None:
        return None
    
    try:
        return float(val)
    except:
        return None


def parse_outcome(raw: Any, market_title: str = "") -> OutcomeType:
    """
    Parse outcome to UP/DOWN/YES/NO.
    
    Checks:
    1. Raw value directly
    2. Market title for BTC Up/Down pattern
    """
    if raw is None:
        return None
    
    raw_lower = str(raw).lower().strip()
    
    # Direct mapping
    if raw_lower in ("up", "yes"):
        return "UP" if "btc" in market_title.lower() or "up" in raw_lower else "YES"
    if raw_lower in ("down", "no"):
        return "DOWN" if "btc" in market_title.lower() or "down" in raw_lower else "NO"
    if raw_lower == "a":
        return "A"
    if raw_lower == "b":
        return "B"
    
    # Check market title for BTC pattern
    title_lower = market_title.lower()
    if "btc" in title_lower or "bitcoin" in title_lower:
        if "up" in raw_lower:
            return "UP"
        if "down" in raw_lower:
            return "DOWN"
    
    return None


def parse_side(val: Any) -> SideType:
    """Parse side to BUY/SELL."""
    if val is None:
        return None
    
    val_lower = str(val).lower().strip()
    
    if val_lower in ("buy", "bid", "long"):
        return "BUY"
    if val_lower in ("sell", "ask", "short"):
        return "SELL"
    
    return None


def extract_window_id(market_id: str, market_title: str = "", ts: datetime = None) -> Optional[str]:
    """
    Extract window ID for 15m BTC markets.
    
    Pattern: btc-updown-15m-<unix_start>
    """
    if not market_id:
        return None
    
    # Check if already a window slug
    if "15m-" in market_id:
        return market_id
    
    # Try to find timestamp in market_id or title
    match = re.search(r'15m[-_](\d{10})', market_id)
    if match:
        return f"btc-updown-15m-{match.group(1)}"
    
    match = re.search(r'15m[-_](\d{10})', market_title)
    if match:
        return f"btc-updown-15m-{match.group(1)}"
    
    # If it's a BTC 15m market, compute from timestamp
    combined = f"{market_id} {market_title}".lower()
    if ("btc" in combined or "bitcoin" in combined) and "15" in combined:
        if ts:
            # Compute window start
            unix_ts = int(ts.timestamp())
            window_start = unix_ts - (unix_ts % 900)
            return f"btc-updown-15m-{window_start}"
    
    return None


def normalize_trade(raw: Dict[str, Any]) -> Event:
    """Normalize a raw trade record."""
    # Extract fields (handle various API response formats)
    ts = parse_timestamp(
        raw.get("timestamp") or 
        raw.get("createdAt") or 
        raw.get("created_at") or
        raw.get("matchedAt") or
        raw.get("executedAt")
    )
    
    if ts is None:
        ts = datetime.now()  # Fallback
    
    market_id = (
        raw.get("conditionId") or 
        raw.get("condition_id") or
        raw.get("marketId") or
        raw.get("market_id") or
        raw.get("assetId") or
        raw.get("asset_id") or
        ""
    )
    
    market_title = raw.get("title") or raw.get("question") or raw.get("marketTitle") or ""
    
    outcome = parse_outcome(
        raw.get("outcome") or raw.get("side") or raw.get("asset"),
        market_title
    )
    
    # Side (BUY/SELL)
    side = parse_side(raw.get("side") or raw.get("type") or raw.get("orderType"))
    
    # If side not in raw, infer from context
    if side is None:
        maker_side = raw.get("makerSide") or raw.get("maker_side")
        taker_side = raw.get("takerSide") or raw.get("taker_side")
        if taker_side:
            side = parse_side(taker_side)
    
    price = parse_price(raw.get("price") or raw.get("avgPrice") or raw.get("avg_price"))
    size = parse_size(raw.get("size") or raw.get("amount") or raw.get("quantity"))
    
    # Cash delta
    cash = None
    if price is not None and size is not None and side is not None:
        if side == "BUY":
            cash = -price * size  # Spent
        else:
            cash = price * size   # Received
    
    tx = raw.get("transactionHash") or raw.get("tx") or raw.get("txHash")
    
    return Event(
        ts=ts,
        kind="TRADE",
        market_id=market_id,
        window_id=extract_window_id(market_id, market_title, ts),
        outcome=outcome,
        side=side,
        price=price,
        size=size,
        cash_delta=cash,
        tx=tx,
        meta=raw,
    )


# Activity type mapping
ACTIVITY_TYPE_MAP = {
    "merge": "MERGE",
    "split": "SPLIT",
    "redeem": "REDEEM",
    "redemption": "REDEEM",
    "claim": "REDEEM",
    "reward": "REWARD",
    "rewards": "REWARD",
    "transfer": "TRANSFER",
    "deposit": "TRANSFER",
    "withdrawal": "TRANSFER",
    "buy": "TRADE",
    "sell": "TRADE",
    "trade": "TRADE",
    "fill": "TRADE",
}


def normalize_activity(raw: Dict[str, Any]) -> Event:
    """Normalize a raw activity record."""
    ts = parse_timestamp(
        raw.get("timestamp") or 
        raw.get("createdAt") or 
        raw.get("created_at") or
        raw.get("blockTimestamp")
    )
    
    if ts is None:
        ts = datetime.now()
    
    # Determine kind
    activity_type = str(raw.get("type") or raw.get("action") or raw.get("activity_type") or "").lower()
    kind = ACTIVITY_TYPE_MAP.get(activity_type, "UNKNOWN")
    
    # Also check description/name
    desc = str(raw.get("description") or raw.get("name") or "").lower()
    if kind == "UNKNOWN":
        for key, val in ACTIVITY_TYPE_MAP.items():
            if key in desc:
                kind = val
                break
    
    market_id = (
        raw.get("conditionId") or 
        raw.get("condition_id") or
        raw.get("marketId") or
        raw.get("market_id") or
        ""
    )
    
    market_title = raw.get("title") or raw.get("question") or raw.get("marketTitle") or ""
    
    outcome = parse_outcome(raw.get("outcome") or raw.get("asset"), market_title)
    
    # Activity may have amount/value
    size = parse_size(raw.get("amount") or raw.get("value") or raw.get("size"))
    
    # Cash delta from activity
    cash = parse_size(raw.get("cashDelta") or raw.get("cash_delta") or raw.get("usdcAmount"))
    if cash and raw.get("direction") == "out":
        cash = -abs(cash)
    
    tx = raw.get("transactionHash") or raw.get("tx") or raw.get("txHash")
    
    return Event(
        ts=ts,
        kind=kind,
        market_id=market_id,
        window_id=extract_window_id(market_id, market_title, ts),
        outcome=outcome,
        side=None,
        price=None,
        size=size,
        cash_delta=cash,
        tx=tx,
        meta=raw,
    )


def normalize_all(
    raw_trades: List[Dict[str, Any]],
    raw_activity: List[Dict[str, Any]],
) -> List[Event]:
    """
    Normalize all raw data into unified Event list.
    
    Returns:
        List of Events sorted by timestamp
    """
    events = []
    
    for raw in raw_trades:
        try:
            events.append(normalize_trade(raw))
        except Exception as e:
            print(f"  Warning: Failed to normalize trade: {e}")
    
    for raw in raw_activity:
        try:
            events.append(normalize_activity(raw))
        except Exception as e:
            print(f"  Warning: Failed to normalize activity: {e}")
    
    # Sort by timestamp
    events.sort(key=lambda e: e.ts)
    
    return events


